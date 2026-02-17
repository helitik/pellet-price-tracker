"""TotalEnergies API crawl logic with retry mechanism."""

import logging
from datetime import date
from decimal import Decimal

import requests
from sqlalchemy.orm import Session

from app.config import Config
from app.models import Crawl, Town

logger = logging.getLogger(__name__)

API_URL = (
    "https://www.proxi-totalenergies.fr/advanced-product-page/"
    "get-product-by-product-category-and-town"
)
REQUEST_TIMEOUT = 30


def crawl_town(session: Session, town: Town) -> Crawl | None:
    """Crawl prices for a given town and insert/update the result in the database."""
    today = date.today()
    quantity = Config.CRAWL_QUANTITY

    # Check if a successful crawl already exists for today
    existing = (
        session.query(Crawl)
        .filter_by(town_id=town.id, crawl_date=today, status="success")
        .first()
    )
    if existing:
        logger.info("Crawl already done for %s on %s, skipping.", town.name, today)
        return existing

    # Look for an existing failed crawl (for update on retry)
    error_crawl = (
        session.query(Crawl)
        .filter_by(town_id=town.id, crawl_date=today, status="error")
        .first()
    )

    http_code = None
    try:
        resp = requests.get(
            API_URL,
            params={
                "productCategory": Config.CRAWL_PRODUCT_CATEGORY,
                "town": town.code,
            },
            timeout=REQUEST_TIMEOUT,
        )
        http_code = resp.status_code
        resp.raise_for_status()
        data = resp.json()

        result = data.get("result")
        if not result or "prices" not in result:
            raise ValueError("Invalid JSON response: 'result.prices' missing")

        prices = result["prices"]
        entry = None
        for p in prices:
            if p.get("quantity") == quantity:
                entry = p
                break

        if entry is None:
            raise ValueError(
                f"No entry found for quantity={quantity}"
            )

        unit_price = Decimal(str(entry["unit_price"]))
        unit_price_with_discount = Decimal(str(entry["unit_price_with_discount"]))
        delivery = Decimal(str(entry["delivery"]))
        flash_sale = bool(result.get("flash", False))

        if error_crawl:
            # Update the existing failed crawl
            error_crawl.unit_price = unit_price
            error_crawl.unit_price_with_discount = unit_price_with_discount
            error_crawl.delivery = delivery
            error_crawl.flash_sale = flash_sale
            error_crawl.status = "success"
            error_crawl.error_message = None
            error_crawl.http_status_code = http_code
            session.commit()
            logger.info(
                "Crawl retry succeeded for %s: %s EUR/t", town.name, unit_price
            )
            return error_crawl
        else:
            crawl = Crawl(
                town_id=town.id,
                crawl_date=today,
                quantity=quantity,
                unit_price=unit_price,
                unit_price_with_discount=unit_price_with_discount,
                delivery=delivery,
                flash_sale=flash_sale,
                status="success",
                http_status_code=http_code,
            )
            session.add(crawl)
            session.commit()
            logger.info(
                "Crawl succeeded for %s: %s EUR/t", town.name, unit_price
            )
            return crawl

    except Exception as e:
        logger.error("Crawl error for %s: %s", town.name, e)
        if isinstance(e, requests.HTTPError) and e.response is not None:
            http_code = e.response.status_code

        if error_crawl:
            error_crawl.error_message = str(e)
            error_crawl.http_status_code = http_code
            session.commit()
        else:
            crawl = Crawl(
                town_id=town.id,
                crawl_date=today,
                quantity=quantity,
                status="error",
                error_message=str(e),
                http_status_code=http_code,
            )
            session.add(crawl)
            session.commit()
        return None


def run_crawl(session_factory) -> list[Crawl]:
    """Run the crawl for all active towns."""
    session: Session = session_factory()
    try:
        towns = session.query(Town).filter_by(active=True).all()
        if not towns:
            logger.warning("No active towns found.")
            return []

        results = []
        for town in towns:
            crawl = crawl_town(session, town)
            if crawl:
                results.append(crawl)
        return results
    except Exception as e:
        logger.error("Global crawl error: %s", e)
        return []
    finally:
        session.close()


def check_and_crawl_today(session_factory) -> None:
    """Check if today's crawl has been done, otherwise run it."""
    session: Session = session_factory()
    try:
        today = date.today()
        towns = session.query(Town).filter_by(active=True).all()

        needs_crawl = False
        for town in towns:
            existing = (
                session.query(Crawl)
                .filter_by(town_id=town.id, crawl_date=today, status="success")
                .first()
            )
            if not existing:
                needs_crawl = True
                break

        if needs_crawl:
            logger.info("Today's crawl missing, running immediately.")
            session.close()
            run_crawl_with_alerts(session_factory)
        else:
            logger.info("Today's crawl already done.")
    except Exception as e:
        logger.error("Error checking today's crawl: %s", e)
    finally:
        session.close()


def run_crawl_with_alerts(session_factory) -> None:
    """Run the crawl then analyze alerts."""
    from app.alerts import analyze_and_notify

    results = run_crawl(session_factory)
    if results:
        session: Session = session_factory()
        try:
            for crawl in results:
                # Re-attach crawl to this session
                crawl = session.merge(crawl)
                analyze_and_notify(session, crawl)
        except Exception as e:
            logger.error("Alert analysis error: %s", e)
        finally:
            session.close()
