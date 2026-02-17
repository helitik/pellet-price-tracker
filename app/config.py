"""Load configuration from environment variables."""

import os


class Config:
    # Database
    MYSQL_HOST: str = os.environ.get("MYSQL_HOST", "db")
    MYSQL_PORT: int = int(os.environ.get("MYSQL_PORT", "3306"))
    MYSQL_DATABASE: str = os.environ.get("MYSQL_DATABASE", "pellet_tracker")
    MYSQL_USER: str = os.environ.get("MYSQL_USER", "pellet")
    MYSQL_PASSWORD: str = os.environ.get("MYSQL_PASSWORD", "changeme")

    # Crawl
    CRAWL_HOUR: int = int(os.environ.get("CRAWL_HOUR", "8"))
    CRAWL_MINUTE: int = int(os.environ.get("CRAWL_MINUTE", "0"))
    CRAWL_PRODUCT_CATEGORY: int = int(os.environ.get("CRAWL_PRODUCT_CATEGORY", "2"))
    CRAWL_QUANTITY: int = int(os.environ.get("CRAWL_QUANTITY", "3"))

    # SMTP
    SMTP_HOST: str = os.environ.get("SMTP_HOST", "smtp.mailgun.org")
    SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER: str = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")
    MAIL_FROM: str = os.environ.get("MAIL_FROM", "pellet-tracker@example.com")
    MAIL_TO: str = os.environ.get("MAIL_TO", "recipient@example.com")

    # Alerts
    PRICE_DROP_THRESHOLD_PERCENT: float = float(
        os.environ.get("PRICE_DROP_THRESHOLD_PERCENT", "5")
    )

    # App
    FLASK_PORT: int = int(os.environ.get("FLASK_PORT", "5000"))

    @classmethod
    def database_url(cls) -> str:
        return (
            f"mysql+pymysql://{cls.MYSQL_USER}:{cls.MYSQL_PASSWORD}"
            f"@{cls.MYSQL_HOST}:{cls.MYSQL_PORT}/{cls.MYSQL_DATABASE}"
            "?charset=utf8mb4"
        )
