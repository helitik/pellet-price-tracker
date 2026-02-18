"""Entrypoint: initializes Flask and the APScheduler scheduler."""

import logging
import time
from threading import Thread

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask
from sqlalchemy import text

from app.config import Config
from app.models import Base, get_engine, get_session_factory
from app.routes import bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def wait_for_db(engine, max_retries: int = 30, delay: int = 2) -> None:
    """Wait for MariaDB to be ready with linear backoff."""
    for attempt in range(1, max_retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database connection established.")
            return
        except Exception as e:
            logger.warning(
                "DB not ready (attempt %d/%d): %s", attempt, max_retries, e
            )
            time.sleep(delay)
    raise RuntimeError("Unable to connect to the database.")


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app


def main() -> None:
    logger.info("Starting Pellet Price Tracker...")

    # DB connection with wait
    engine = get_engine()
    wait_for_db(engine)

    # Create tables
    Base.metadata.create_all(engine)
    logger.info("Tables created / verified.")

    session_factory = get_session_factory(engine)

    # Check if today's crawl has been done
    from app.crawler import check_and_crawl_today, run_crawl_with_alerts

    Thread(target=check_and_crawl_today, args=(session_factory,), daemon=True).start()

    # APScheduler setup
    scheduler = BackgroundScheduler(timezone="Europe/Paris")
    scheduler.add_job(
        run_crawl_with_alerts,
        CronTrigger(hour=Config.CRAWL_HOUR, minute=Config.CRAWL_MINUTE),
        args=[session_factory],
        id="daily_crawl",
        name="Daily pellet price crawl",
        misfire_grace_time=3600,
    )

    # Retry every 15 minutes (max 5 attempts = 1h15 after the scheduled crawl)
    scheduler.add_job(
        _retry_failed_crawl,
        CronTrigger(minute="*/15"),
        args=[session_factory],
        id="retry_crawl",
        name="Retry failed crawl",
        misfire_grace_time=900,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: daily crawl at %02d:%02d Europe/Paris.",
        Config.CRAWL_HOUR,
        Config.CRAWL_MINUTE,
    )

    # Flask
    app = create_app()
    app.run(host="0.0.0.0", port=Config.FLASK_PORT, debug=False)


def _retry_failed_crawl(session_factory) -> None:
    """Check for failed crawls today and retry (max 5 attempts).

    Uses a time window: retry only within 75 minutes
    (5 x 15 min) after the scheduled crawl time.
    """
    from datetime import date, datetime, timedelta

    import pytz

    from app.crawler import run_crawl_with_alerts
    from app.models import Crawl

    session = session_factory()
    try:
        tz = pytz.timezone("Europe/Paris")
        now = datetime.now(tz)
        today = now.date()

        # Check if there's already a success today
        has_success = (
            session.query(Crawl)
            .filter(Crawl.crawl_date == today, Crawl.status == "success")
            .first()
        )
        if has_success:
            return

        # Check if there's a failed crawl to retry
        has_error = (
            session.query(Crawl)
            .filter(Crawl.crawl_date == today, Crawl.status == "error")
            .first()
        )
        if not has_error:
            return

        # Only retry within the 75 min window after the scheduled crawl time
        scheduled = tz.localize(
            datetime(today.year, today.month, today.day, Config.CRAWL_HOUR, Config.CRAWL_MINUTE)
        )
        retry_window_end = scheduled + timedelta(minutes=75)

        if now > retry_window_end:
            logger.warning("Retry window expired for crawl on %s.", today)
            return

        logger.info("Retrying crawl...")
        session.close()
        run_crawl_with_alerts(session_factory)
    except Exception as e:
        logger.error("Crawl retry error: %s", e)
    finally:
        session.close()


if __name__ == "__main__":
    main()
