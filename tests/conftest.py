"""Shared fixtures: in-memory SQLite database, a town, and crawl helpers."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import alerts
from app.models import Base, Crawl, Town

# Fixed reference date for deterministic scenarios (day 0)
DAY0 = date(2026, 6, 1)


def day(n: int) -> date:
    """Return DAY0 + n days."""
    return DAY0 + timedelta(days=n)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def town(session):
    t = Town(code="71761", name="Hadol")
    session.add(t)
    session.commit()
    return t


@pytest.fixture
def add_crawl(session, town):
    """Insert a crawl. `discount` defaults to the unit price (no discount)."""

    def _add(crawl_date, price=None, discount=None, status="success", **kwargs):
        unit_price = Decimal(str(price)) if price is not None else None
        if unit_price is None:
            unit_price_with_discount = None
        else:
            unit_price_with_discount = (
                Decimal(str(discount)) if discount is not None else unit_price
            )
        crawl = Crawl(
            town_id=town.id,
            crawl_date=crawl_date,
            quantity=3,
            unit_price=unit_price,
            unit_price_with_discount=unit_price_with_discount,
            delivery=Decimal("0"),
            flash_sale=discount is not None,
            status=status,
            **kwargs,
        )
        session.add(crawl)
        session.commit()
        return crawl

    return _add


@pytest.fixture
def stable_history(add_crawl):
    """30 days of stable price at 380 EUR/t (days 0..29)."""
    for i in range(30):
        add_crawl(day(i), "380")


@pytest.fixture(autouse=True)
def alert_config(monkeypatch):
    """Pin the alert threshold and disable SMTP so no real email is attempted."""
    monkeypatch.setattr(alerts.Config, "PRICE_DROP_THRESHOLD_PERCENT", 5.0)
    monkeypatch.setattr(alerts.Config, "SMTP_USER", "")
    monkeypatch.setattr(alerts.Config, "SMTP_PASSWORD", "")


@pytest.fixture
def sent_emails(monkeypatch):
    """Replace send_alert_email with a recorder that reports success.

    Each entry is (crawl_date, [alert types], avg_30d, min_6m).
    """
    calls = []

    def fake_send(crawl, alert_list, avg_30d, min_6m, town_name=""):
        calls.append(
            (crawl.crawl_date, [a["type"] for a in alert_list], avg_30d, min_6m)
        )
        return True

    monkeypatch.setattr(alerts, "send_alert_email", fake_send)
    return calls
