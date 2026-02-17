"""Price alert detection and email notification sending."""

import logging
import smtplib
from datetime import date, timedelta
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import Config
from app.models import Crawl, Notification

logger = logging.getLogger(__name__)


def analyze_and_notify(session: Session, crawl: Crawl) -> None:
    """Analyze prices from a successful crawl and send alerts if needed."""
    if crawl.status != "success" or crawl.unit_price is None:
        return

    # Avoid duplicates if analyze_and_notify is called twice for the same crawl
    existing = session.query(Notification).filter_by(crawl_id=crawl.id).first()
    if existing:
        logger.info("Notifications already created for crawl #%s, skipping.", crawl.id)
        return

    alerts: list[dict] = []
    today = crawl.crawl_date
    town_name = crawl.town.name

    # 1. Lowest price (6-month window)
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

    if min_price_row is not None:
        min_price = Decimal(str(min_price_row))
        if crawl.unit_price <= min_price:
            alerts.append(
                {
                    "type": "lowest_price",
                    "current_price": crawl.unit_price,
                    "reference_price": min_price,
                }
            )
            logger.info(
                "[%s] Alert lowest_price: %s <= %s (6-month min)",
                town_name,
                crawl.unit_price,
                min_price,
            )

    # 2. Drop vs. 30-day average
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

    if avg_price_row is not None:
        avg_price = Decimal(str(avg_price_row)).quantize(Decimal("0.01"))
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
            logger.info(
                "[%s] Alert price_drop: %s < %s (30-day avg - %s%%)",
                town_name,
                crawl.unit_price,
                drop_limit,
                threshold,
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
        logger.info(
            "[%s] Alert discount_active: discount %s -> %s",
            town_name,
            crawl.unit_price,
            crawl.unit_price_with_discount,
        )

    if not alerts:
        logger.info("[%s] No alert detected for crawl #%s.", town_name, crawl.id)
        return

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
    sent = send_alert_email(crawl, alerts, avg_price_row, min_price_row, town_name)

    for notif in notifications:
        notif.sent = sent
    session.commit()


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
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.sendmail(Config.MAIL_FROM, [Config.MAIL_TO], msg.as_string())
        logger.info("Alert email sent to %s.", Config.MAIL_TO)
        return True
    except Exception as e:
        logger.error("Email sending error: %s", e)
        return False
