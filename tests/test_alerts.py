"""Tests for alert detection, event-based notification and email retry."""

import email
import email.header
import smtplib
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app import alerts
from app.models import Notification
from tests.conftest import day


def notified_types(session, crawl):
    return sorted(n.alert_type for n in session.query(Notification).filter_by(crawl_id=crawl.id))


# ---------------------------------------------------------------------------
# detect_alerts: pure detection
# ---------------------------------------------------------------------------


def test_no_alert_when_price_is_stable(session, add_crawl, stable_history):
    crawl = add_crawl(day(30), "380")
    found, min_6m, avg_30d = alerts.detect_alerts(session, crawl)
    assert found == []
    assert min_6m == Decimal("380")
    assert avg_30d == Decimal("380.00")


def test_price_drop_and_lowest_price_detected(session, add_crawl, stable_history):
    crawl = add_crawl(day(30), "350")  # -7.9% vs 30-day average
    found, _, _ = alerts.detect_alerts(session, crawl)
    assert sorted(a["type"] for a in found) == ["lowest_price", "price_drop"]
    drop = next(a for a in found if a["type"] == "price_drop")
    assert drop["current_price"] == Decimal("350")
    assert drop["reference_price"] == Decimal("380.00")


def test_drop_below_threshold_is_ignored(session, add_crawl, stable_history):
    crawl = add_crawl(day(30), "365")  # -3.9%, under the 5% threshold, but a 6-month low
    found, _, _ = alerts.detect_alerts(session, crawl)
    assert [a["type"] for a in found] == ["lowest_price"]


def test_unchanged_lowest_price_is_not_a_new_low(session, add_crawl, stable_history):
    add_crawl(day(30), "350")
    crawl = add_crawl(day(31), "350")
    found, _, _ = alerts.detect_alerts(session, crawl)
    assert "lowest_price" not in [a["type"] for a in found]


def test_discount_detected(session, add_crawl, stable_history):
    crawl = add_crawl(day(30), "380", discount="342")
    found, _, _ = alerts.detect_alerts(session, crawl)
    assert [a["type"] for a in found] == ["discount_active"]
    assert found[0]["current_price"] == Decimal("342")
    assert found[0]["reference_price"] == Decimal("380")


def test_no_history_only_discount_can_fire(session, add_crawl):
    crawl = add_crawl(day(0), "380", discount="360")
    found, min_6m, avg_30d = alerts.detect_alerts(session, crawl)
    assert min_6m is None and avg_30d is None
    assert [a["type"] for a in found] == ["discount_active"]


def test_failed_crawls_are_excluded_from_references(session, add_crawl, stable_history):
    add_crawl(day(30), status="error", error_message="boom")
    crawl = add_crawl(day(31), "350")
    found, min_6m, avg_30d = alerts.detect_alerts(session, crawl)
    assert min_6m == Decimal("380")
    assert avg_30d == Decimal("380.00")
    assert sorted(a["type"] for a in found) == ["lowest_price", "price_drop"]


# ---------------------------------------------------------------------------
# filter_new_alerts: what counts as a new event
# ---------------------------------------------------------------------------


def _alert(kind, price):
    return {"type": kind, "current_price": Decimal(price), "reference_price": Decimal("400")}


def test_filter_keeps_new_type():
    current = [_alert("price_drop", "350"), _alert("discount_active", "330")]
    previous = [_alert("price_drop", "350")]
    assert alerts.filter_new_alerts(current, previous) == [_alert("discount_active", "330")]


def test_filter_drops_unchanged_condition():
    current = [_alert("discount_active", "330")]
    assert alerts.filter_new_alerts(current, [_alert("discount_active", "330")]) == []


def test_filter_keeps_further_drop():
    current = [_alert("price_drop", "340")]
    assert alerts.filter_new_alerts(current, [_alert("price_drop", "350")]) == current


def test_filter_drops_price_increase_still_under_condition():
    current = [_alert("price_drop", "355")]
    assert alerts.filter_new_alerts(current, [_alert("price_drop", "350")]) == []


def test_filter_without_previous_keeps_everything():
    current = [_alert("price_drop", "350"), _alert("lowest_price", "350")]
    assert alerts.filter_new_alerts(current, []) == current


# ---------------------------------------------------------------------------
# analyze_and_notify: one email per event, not per day
# ---------------------------------------------------------------------------


def test_price_drop_emailed_once_until_further_drop(session, add_crawl, stable_history, sent_emails):
    c30 = add_crawl(day(30), "350")
    alerts.analyze_and_notify(session, c30)
    assert notified_types(session, c30) == ["lowest_price", "price_drop"]

    c31 = add_crawl(day(31), "350")  # same price, condition still true
    alerts.analyze_and_notify(session, c31)
    assert notified_types(session, c31) == []

    c32 = add_crawl(day(32), "340")  # further drop
    alerts.analyze_and_notify(session, c32)
    assert notified_types(session, c32) == ["lowest_price", "price_drop"]

    assert [c[0] for c in sent_emails] == [day(30), day(32)]


def test_discount_emailed_once_per_promotion(session, add_crawl, stable_history, sent_emails):
    timeline = [
        (30, "380", "342", ["discount_active"]),  # promotion starts
        (31, "380", "342", []),  # still running
        (32, "380", "342", []),
        (33, "380", "330", ["discount_active"]),  # better discount
        (34, "380", None, []),  # promotion over
        (35, "380", "342", ["discount_active"]),  # new promotion
        (36, "380", "342", []),
    ]
    for n, price, discount, expected in timeline:
        crawl = add_crawl(day(n), price, discount=discount)
        alerts.analyze_and_notify(session, crawl)
        assert notified_types(session, crawl) == expected, f"day {n}"

    assert [c[0] for c in sent_emails] == [day(30), day(33), day(35)]


def test_gap_of_failed_crawls_does_not_renotify(session, add_crawl, stable_history, sent_emails):
    c30 = add_crawl(day(30), "380", discount="342")
    alerts.analyze_and_notify(session, c30)
    add_crawl(day(31), status="error", error_message="timeout")
    add_crawl(day(32), status="error", error_message="timeout")

    c33 = add_crawl(day(33), "380", discount="342")
    alerts.analyze_and_notify(session, c33)

    assert notified_types(session, c33) == []
    assert len(sent_emails) == 1


def test_email_groups_all_new_alerts_of_the_day(session, add_crawl, stable_history, sent_emails):
    crawl = add_crawl(day(30), "350", discount="330")
    alerts.analyze_and_notify(session, crawl)
    assert len(sent_emails) == 1
    _, types, avg_30d, min_6m = sent_emails[0]
    assert sorted(types) == ["discount_active", "lowest_price", "price_drop"]
    assert avg_30d == Decimal("380.00")
    assert min_6m == Decimal("380")


def test_analyze_is_idempotent(session, add_crawl, stable_history, sent_emails):
    c30 = add_crawl(day(30), "350")
    alerts.analyze_and_notify(session, c30)
    alerts.analyze_and_notify(session, c30)  # e.g. startup catch-up re-running the day
    assert session.query(Notification).filter_by(crawl_id=c30.id).count() == 2
    assert len(sent_emails) == 1

    c31 = add_crawl(day(31), "350")  # suppressed day, re-analysed twice
    alerts.analyze_and_notify(session, c31)
    alerts.analyze_and_notify(session, c31)
    assert notified_types(session, c31) == []
    assert len(sent_emails) == 1


def test_error_crawl_is_ignored(session, add_crawl, sent_emails):
    crawl = add_crawl(day(0), status="error", error_message="boom")
    alerts.analyze_and_notify(session, crawl)
    assert session.query(Notification).count() == 0
    assert sent_emails == []


def test_notifications_marked_unsent_when_email_fails(session, add_crawl, stable_history):
    # SMTP is not configured by the autouse fixture: send_alert_email returns False
    crawl = add_crawl(day(30), "350")
    alerts.analyze_and_notify(session, crawl)
    notifs = session.query(Notification).filter_by(crawl_id=crawl.id).all()
    assert len(notifs) == 2
    assert all(n.sent is False for n in notifs)


# ---------------------------------------------------------------------------
# retry_unsent_notifications
# ---------------------------------------------------------------------------


@pytest.fixture
def smtp_configured(monkeypatch):
    monkeypatch.setattr(alerts.Config, "SMTP_USER", "user")
    monkeypatch.setattr(alerts.Config, "SMTP_PASSWORD", "secret")


def _unsent(session, crawl, age_minutes, kind="discount_active"):
    notif = Notification(
        crawl_id=crawl.id,
        alert_type=kind,
        current_price=Decimal("342"),
        reference_price=Decimal("380"),
        sent=False,
        created_at=datetime.now() - timedelta(minutes=age_minutes),
    )
    session.add(notif)
    session.commit()
    return notif


def test_retry_resends_todays_failed_email(session, session_factory, add_crawl, smtp_configured, sent_emails):
    today = date.today()
    add_crawl(today - timedelta(days=1), "380")
    crawl = add_crawl(today, "380", discount="342")
    notif = _unsent(session, crawl, age_minutes=10)

    alerts.retry_unsent_notifications(session_factory)

    session.expire_all()
    assert session.get(Notification, notif.id).sent is True
    assert sent_emails == [(today, ["discount_active"], Decimal("380.00"), Decimal("380"))]


def test_retry_groups_notifications_of_same_crawl(session, session_factory, add_crawl, smtp_configured, sent_emails):
    crawl = add_crawl(date.today(), "350", discount="330")
    _unsent(session, crawl, 10, kind="price_drop")
    _unsent(session, crawl, 10, kind="discount_active")

    alerts.retry_unsent_notifications(session_factory)

    assert len(sent_emails) == 1
    assert sorted(sent_emails[0][1]) == ["discount_active", "price_drop"]
    session.expire_all()
    assert all(n.sent for n in session.query(Notification).all())


def test_retry_leaves_recent_notification_alone(session, session_factory, add_crawl, smtp_configured, sent_emails):
    crawl = add_crawl(date.today(), "380", discount="342")
    notif = _unsent(session, crawl, age_minutes=1)

    alerts.retry_unsent_notifications(session_factory)

    assert sent_emails == []
    session.expire_all()
    assert session.get(Notification, notif.id).sent is False


def test_retry_ignores_past_days(session, session_factory, add_crawl, smtp_configured, sent_emails):
    crawl = add_crawl(date.today() - timedelta(days=1), "380", discount="342")
    _unsent(session, crawl, age_minutes=60 * 24)

    alerts.retry_unsent_notifications(session_factory)

    assert sent_emails == []


def test_retry_keeps_unsent_when_sending_fails_again(session, session_factory, add_crawl, smtp_configured, monkeypatch):
    monkeypatch.setattr(alerts, "send_alert_email", lambda *a, **k: False)
    crawl = add_crawl(date.today(), "380", discount="342")
    notif = _unsent(session, crawl, age_minutes=10)

    alerts.retry_unsent_notifications(session_factory)

    session.expire_all()
    assert session.get(Notification, notif.id).sent is False


def test_retry_is_noop_without_smtp_config(session, session_factory, add_crawl, sent_emails):
    crawl = add_crawl(date.today(), "380", discount="342")
    _unsent(session, crawl, age_minutes=10)

    alerts.retry_unsent_notifications(session_factory)

    assert sent_emails == []


# ---------------------------------------------------------------------------
# send_alert_email: message content and SMTP handling
# ---------------------------------------------------------------------------


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.sent = []
        self.logged_in = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        self.logged_in = (user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, to_addrs, msg))


@pytest.fixture
def fake_smtp(monkeypatch, smtp_configured):
    FakeSMTP.instances = []
    monkeypatch.setattr(alerts.Config, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(alerts.Config, "SMTP_PORT", 2525)
    monkeypatch.setattr(alerts.Config, "MAIL_FROM", "tracker@test")
    monkeypatch.setattr(alerts.Config, "MAIL_TO", "me@test")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_send_alert_email_builds_grouped_message(session, add_crawl, fake_smtp):
    crawl = add_crawl(day(30), "350", discount="330")
    found = [
        _alert("lowest_price", "350"),
        _alert("price_drop", "350"),
        {"type": "discount_active", "current_price": Decimal("330"), "reference_price": Decimal("350")},
    ]

    ok = alerts.send_alert_email(crawl, found, Decimal("380.00"), Decimal("380"), "Hadol")

    assert ok is True
    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.test", 2525, alerts.SMTP_TIMEOUT)
    assert smtp.logged_in == ("user", "secret")
    from_addr, to_addrs, raw = smtp.sent[0]
    assert (from_addr, to_addrs) == ("tracker@test", ["me@test"])
    msg = email.message_from_string(raw)
    subject = str(email.header.make_header(email.header.decode_header(msg["Subject"])))
    assert subject == "[Pellet Tracker] Alerte prix — Hadol — 2026-07-01"
    html = next(
        part.get_payload(decode=True).decode("utf-8")
        for part in msg.walk()
        if part.get_content_type() == "text/html"
    )
    assert "Hadol" in html and "2026-07-01" in html
    assert "Prix au plus bas sur 6 mois" in html
    assert "Baisse significative vs moyenne 30 jours" in html
    assert "Remise active" in html
    assert "-12.5%" in html  # (400 - 350) / 400
    assert "Moyenne 30j : 380.00" in html and "Min 6 mois : 380" in html


def test_send_alert_email_returns_false_on_smtp_error(session, add_crawl, fake_smtp, monkeypatch):
    def broken(*args, **kwargs):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(smtplib, "SMTP", broken)
    crawl = add_crawl(day(30), "380", discount="342")
    ok = alerts.send_alert_email(crawl, [_alert("discount_active", "342")], None, None, "Hadol")
    assert ok is False


def test_send_alert_email_skipped_without_smtp_config(session, add_crawl, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP must not be used"))
    crawl = add_crawl(day(30), "380", discount="342")
    assert alerts.send_alert_email(crawl, [_alert("discount_active", "342")], None, None) is False
