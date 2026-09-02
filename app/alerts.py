"""Price alert detection and email notification sending.

Alerts are *event*-based: an email is only sent when a condition appears
(or gets better, i.e. the price drops further), not every day the condition
holds. To decide, the same detection is run on the previous successful crawl
of the town and the two results are compared.
"""

import logging
import smtplib
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import Config
from app.models import Crawl, Notification

logger = logging.getLogger(__name__)

SMTP_TIMEOUT = 30
# Do not retry a notification created less than this many minutes ago:
# it may still be in the middle of its first sending attempt.
RETRY_MIN_AGE_MINUTES = 5


def _reference_prices(
    session: Session, crawl: Crawl
) -> tuple[Decimal | None, Decimal | None]:
    """Return (6-month min, 30-day avg) of successful crawls strictly before this one."""
    today = crawl.crawl_date

    six_months_ago = today - timedelta(days=180)
    min_price_row = (
        session.query(func.min(Crawl.unit_price))
        .filter(
            Crawl.town_id == crawl.town_id,
            Crawl.status == "success",
            Crawl.crawl_date >= six_months_ago,
            Crawl.crawl_date < today,
            Crawl.unit_price.isnot(None),
        )
        .scalar()
    )

    thirty_days_ago = today - timedelta(days=30)
    avg_price_row = (
        session.query(func.avg(Crawl.unit_price))
        .filter(
            Crawl.town_id == crawl.town_id,
            Crawl.status == "success",
            Crawl.crawl_date >= thirty_days_ago,
            Crawl.crawl_date < today,
            Crawl.unit_price.isnot(None),
        )
        .scalar()
    )

    min_price = Decimal(str(min_price_row)) if min_price_row is not None else None
    avg_price = (
        Decimal(str(avg_price_row)).quantize(Decimal("0.01"))
        if avg_price_row is not None
        else None
    )
    return min_price, avg_price


def detect_alerts(
    session: Session, crawl: Crawl
) -> tuple[list[dict], Decimal | None, Decimal | None]:
    """Evaluate the alert conditions for a crawl.

    Pure detection, no side effects. Returns (alerts, min_6m, avg_30d).
    """
    alerts: list[dict] = []
    min_price, avg_price = _reference_prices(session, crawl)

    # 1. Lowest price (6-month window)
    if min_price is not None and crawl.unit_price < min_price:
        alerts.append(
            {
                "type": "lowest_price",
                "current_price": crawl.unit_price,
                "reference_price": min_price,
            }
        )

    # 2. Drop vs. 30-day average
    if avg_price is not None:
        threshold = Decimal(str(Config.PRICE_DROP_THRESHOLD_PERCENT))
        drop_limit = avg_price * (1 - threshold / 100)
        if crawl.unit_price < drop_limit:
            alerts.append(
                {
                    "type": "price_drop",
                    "current_price": crawl.unit_price,
                    "reference_price": avg_price,
                }
            )

    # 3. Active discount
    if (
        crawl.unit_price_with_discount is not None
        and crawl.unit_price_with_discount < crawl.unit_price
    ):
        alerts.append(
            {
                "type": "discount_active",
                "current_price": crawl.unit_price_with_discount,
                "reference_price": crawl.unit_price,
            }
        )

    return alerts, min_price, avg_price


def filter_new_alerts(current: list[dict], previous: list[dict]) -> list[dict]:
    """Keep only alerts that are new compared to the previous crawl.

    An alert is new if its type was not raised for the previous crawl, or if
    the price is strictly lower than it was then (a further drop is worth a
    new email; an unchanged situation is not).
    """
    previous_by_type = {a["type"]: a for a in previous}
    new_alerts = []
    for alert in current:
        prev = previous_by_type.get(alert["type"])
        if prev is None or alert["current_price"] < prev["current_price"]:
            new_alerts.append(alert)
    return new_alerts


def _previous_successful_crawl(session: Session, crawl: Crawl) -> Crawl | None:
    return (
        session.query(Crawl)
        .filter(
            Crawl.town_id == crawl.town_id,
            Crawl.status == "success",
            Crawl.unit_price.isnot(None),
            Crawl.crawl_date < crawl.crawl_date,
        )
        .order_by(Crawl.crawl_date.desc())
        .first()
    )


def analyze_and_notify(session: Session, crawl: Crawl) -> None:
    """Analyze prices from a successful crawl and send alerts if needed."""
    if crawl.status != "success" or crawl.unit_price is None:
        return

    # Avoid duplicates if analyze_and_notify is called twice for the same crawl
    existing = session.query(Notification).filter_by(crawl_id=crawl.id).first()
    if existing:
        logger.info("Notifications already created for crawl #%s, skipping.", crawl.id)
        return

    town_name = crawl.town.name
    alerts, min_price, avg_price = detect_alerts(session, crawl)

    if not alerts:
        logger.info("[%s] No alert detected for crawl #%s.", town_name, crawl.id)
        return

    # Only notify what changed since the previous crawl
    previous = _previous_successful_crawl(session, crawl)
    if previous is not None:
        previous_alerts, _, _ = detect_alerts(session, previous)
        new_alerts = filter_new_alerts(alerts, previous_alerts)
        for alert in alerts:
            if alert not in new_alerts:
                logger.info(
                    "[%s] Alert %s still active (%s EUR/t) but already notified "
                    "on %s, no new email.",
                    town_name,
                    alert["type"],
                    alert["current_price"],
                    previous.crawl_date,
                )
        alerts = new_alerts

    if not alerts:
        return

    for alert in alerts:
        logger.info(
            "[%s] Alert %s: %s EUR/t (reference %s EUR/t)",
            town_name,
            alert["type"],
            alert["current_price"],
            alert["reference_price"],
        )

    # Save notifications
    notifications = []
    for alert in alerts:
        notif = Notification(
            crawl_id=crawl.id,
            alert_type=alert["type"],
            current_price=alert["current_price"],
            reference_price=alert["reference_price"],
            sent=False,
        )
        session.add(notif)
        notifications.append(notif)
    session.commit()

    # Send the email
    sent = send_alert_email(crawl, alerts, avg_price, min_price, town_name)

    for notif in notifications:
        notif.sent = sent
    session.commit()


def retry_unsent_notifications(session_factory) -> None:
    """Resend today's alert emails whose sending failed.

    Only notifications from today's crawls are retried, so an old failure
    never turns into a stale alert days later.
    """
    if not Config.SMTP_USER or not Config.SMTP_PASSWORD:
        return

    session: Session = session_factory()
    try:
        today = date.today()
        min_age = datetime.now() - timedelta(minutes=RETRY_MIN_AGE_MINUTES)
        unsent = (
            session.query(Notification)
            .join(Crawl)
            .filter(
                Notification.sent.is_(False),
                Notification.created_at < min_age,
                Crawl.crawl_date == today,
            )
            .order_by(Notification.crawl_id, Notification.id)
            .all()
        )
        if not unsent:
            return

        by_crawl: dict[int, list[Notification]] = {}
        for notif in unsent:
            by_crawl.setdefault(notif.crawl_id, []).append(notif)

        for crawl_id, notifs in by_crawl.items():
            crawl = notifs[0].crawl
            town_name = crawl.town.name
            logger.info(
                "[%s] Retrying alert email for crawl #%s (%d notification(s)).",
                town_name,
                crawl_id,
                len(notifs),
            )
            alerts = [
                {
                    "type": n.alert_type,
                    "current_price": n.current_price,
                    "reference_price": n.reference_price,
                }
                for n in notifs
            ]
            min_price, avg_price = _reference_prices(session, crawl)
            if send_alert_email(crawl, alerts, avg_price, min_price, town_name):
                for n in notifs:
                    n.sent = True
                session.commit()
    except Exception as e:
        logger.error("Notification retry error: %s", e)
    finally:
        session.close()


def send_alert_email(
    crawl: Crawl,
    alerts: list[dict],
    avg_30d: Decimal | None,
    min_6m: Decimal | None,
    town_name: str = "",
) -> bool:
    """Send an HTML email grouping all detected alerts."""
    if not Config.SMTP_USER or not Config.SMTP_PASSWORD:
        logger.warning("SMTP not configured, email not sent.")
        return False

    today_str = crawl.crawl_date.isoformat()
    subject = f"[Pellet Tracker] Alerte prix — {town_name} — {today_str}"

    # Build HTML body
    alert_blocks = []
    for alert in alerts:
        if alert["type"] == "lowest_price":
            alert_blocks.append(
                f'<div style="background:#e8f5e9;padding:12px;border-radius:6px;margin-bottom:10px;">'
                f"<strong>Prix au plus bas sur 6 mois !</strong><br>"
                f'Prix actuel : <strong>{alert["current_price"]} &euro;/t</strong><br>'
                f'Minimum 6 mois : {alert["reference_price"]} &euro;/t'
                f"</div>"
            )
        elif alert["type"] == "price_drop":
            drop_pct = (
                (alert["reference_price"] - alert["current_price"])
                / alert["reference_price"]
                * 100
            )
            alert_blocks.append(
                f'<div style="background:#fff3e0;padding:12px;border-radius:6px;margin-bottom:10px;">'
                f"<strong>Baisse significative vs moyenne 30 jours !</strong><br>"
                f'Prix actuel : <strong>{alert["current_price"]} &euro;/t</strong><br>'
                f'Moyenne 30j : {alert["reference_price"]} &euro;/t<br>'
                f"Baisse : -{drop_pct:.1f}%"
                f"</div>"
            )
        elif alert["type"] == "discount_active":
            discount = alert["reference_price"] - alert["current_price"]
            alert_blocks.append(
                f'<div style="background:#e3f2fd;padding:12px;border-radius:6px;margin-bottom:10px;">'
                f"<strong>Remise active !</strong><br>"
                f'Prix standard : {alert["reference_price"]} &euro;/t<br>'
                f'Prix remis&eacute; : <strong>{alert["current_price"]} &euro;/t</strong><br>'
                f"&Eacute;conomie : {discount} &euro;/t"
                f"</div>"
            )

    body = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
        <h2 style="color:#333;">Pellet Tracker — {town_name} — Alertes du {today_str}</h2>
        {''.join(alert_blocks)}
        <hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">
        <p style="color:#666;font-size:13px;">
            Prix unitaire : {crawl.unit_price} &euro;/t |
            Prix remis&eacute; : {crawl.unit_price_with_discount} &euro;/t |
            Livraison : {crawl.delivery} &euro;
        </p>
        <p style="color:#666;font-size:13px;">
            Moyenne 30j : {avg_30d if avg_30d else 'N/A'} &euro;/t |
            Min 6 mois : {min_6m if min_6m else 'N/A'} &euro;/t
        </p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = Config.MAIL_FROM
    msg["To"] = Config.MAIL_TO
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            server.starttls()
            server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.sendmail(Config.MAIL_FROM, [Config.MAIL_TO], msg.as_string())
        logger.info("Alert email sent to %s.", Config.MAIL_TO)
        return True
    except Exception as e:
        logger.error("Email sending error: %s", e)
        return False
