"""Seed script: generate 365 days of realistic pellet price data."""

import random
import math
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models import Base, Crawl, Town, get_engine, get_session_factory


def seasonal_price(day_of_year: int) -> float:
    """Generate a base price with seasonal variation.

    Pellet prices are higher in heating season (Oct-Mar) and lower in summer.
    Typical bulk price range: ~300-430 €/tonne.
    """
    # Cosine curve: peak around Jan (day ~15), trough around Jul (day ~195)
    seasonal = math.cos(2 * math.pi * (day_of_year - 15) / 365)
    base = 355 + 40 * seasonal  # ~315 in summer, ~395 in winter
    return base


def generate_seed_data():
    engine = get_engine()
    Base.metadata.create_all(engine)
    SessionFactory = get_session_factory(engine)
    session = SessionFactory()

    try:
        # Upsert a default town
        town = session.query(Town).filter_by(code="33063").first()
        if not town:
            town = Town(
                code="33063",
                name="Bordeaux (33000)",
                active=True,
                created_at=datetime.now(),
            )
            session.add(town)
            session.flush()
            print(f"Created town: {town.name} (id={town.id})")
        else:
            print(f"Using existing town: {town.name} (id={town.id})")

        today = date.today()
        start_date = today - timedelta(days=364)
        quantity = 3

        # Slow random walk for multi-day trends
        trend = 0.0
        inserted = 0
        skipped = 0

        for i in range(365):
            current_date = start_date + timedelta(days=i)

            # Skip if crawl already exists for this town+date
            existing = (
                session.query(Crawl)
                .filter_by(town_id=town.id, crawl_date=current_date)
                .first()
            )
            if existing:
                skipped += 1
                continue

            day_of_year = current_date.timetuple().tm_yday

            # ~2% chance of an error day (API down, timeout, etc.)
            if random.random() < 0.02:
                crawl = Crawl(
                    town_id=town.id,
                    crawl_date=current_date,
                    quantity=quantity,
                    unit_price=None,
                    unit_price_with_discount=None,
                    delivery=None,
                    flash_sale=False,
                    status="error",
                    error_message=random.choice([
                        "Connection timeout after 30s",
                        "HTTP 503 Service Unavailable",
                        "HTTP 502 Bad Gateway",
                        "JSON decode error: unexpected token",
                    ]),
                    http_status_code=random.choice([None, 502, 503, 504]),
                    created_at=datetime(
                        current_date.year, current_date.month, current_date.day, 8, 0
                    ),
                )
                session.add(crawl)
                inserted += 1
                continue

            # Price generation
            base = seasonal_price(day_of_year)

            # Random walk trend (momentum)
            trend += random.gauss(0, 0.8)
            trend = max(-15, min(15, trend))  # clamp

            # Daily noise
            noise = random.gauss(0, 3)

            unit_price = round(base + trend + noise, 2)
            unit_price = max(280.0, min(450.0, unit_price))  # hard bounds

            # Discount: ~15% of days have a discount (1-5% off)
            has_discount = random.random() < 0.15
            if has_discount:
                discount_pct = random.uniform(0.01, 0.05)
                discounted = round(unit_price * (1 - discount_pct), 2)
            else:
                discounted = unit_price

            # Flash sale: ~3% of days, always with a bigger discount
            flash_sale = random.random() < 0.03
            if flash_sale:
                discount_pct = random.uniform(0.05, 0.10)
                discounted = round(unit_price * (1 - discount_pct), 2)

            # Delivery: 50-80€, slight seasonal variation
            delivery = round(55 + 10 * math.cos(2 * math.pi * day_of_year / 365) + random.gauss(0, 3), 2)
            delivery = max(40.0, min(90.0, delivery))

            crawl = Crawl(
                town_id=town.id,
                crawl_date=current_date,
                quantity=quantity,
                unit_price=Decimal(str(unit_price)),
                unit_price_with_discount=Decimal(str(discounted)),
                delivery=Decimal(str(delivery)),
                flash_sale=flash_sale,
                status="success",
                error_message=None,
                http_status_code=200,
                created_at=datetime(
                    current_date.year, current_date.month, current_date.day, 8, 0
                ),
            )
            session.add(crawl)
            inserted += 1

        session.commit()
        print(f"Seed complete: {inserted} crawls inserted, {skipped} skipped (already existed)")
        print(f"Date range: {start_date} -> {today}")

    except Exception as e:
        session.rollback()
        print(f"Error: {e}", file=sys.stderr)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    generate_seed_data()

