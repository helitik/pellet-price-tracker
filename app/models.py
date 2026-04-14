"""SQLAlchemy models for pellet price tracking."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from app.config import Config


class Base(DeclarativeBase):
    pass


class Town(Base):
    __tablename__ = "towns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    crawls = relationship("Crawl", back_populates="town")


class Crawl(Base):
    __tablename__ = "crawls"
    __table_args__ = (
        UniqueConstraint("town_id", "crawl_date", name="uq_town_crawl_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    town_id = Column(Integer, ForeignKey("towns.id"), nullable=False)
    crawl_date = Column(Date, nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Numeric(10, 2), nullable=True)
    unit_price_with_discount = Column(Numeric(10, 2), nullable=True)
    delivery = Column(Numeric(10, 2), nullable=True)
    flash_sale = Column(Boolean, default=False, nullable=False)
    status = Column(
        Enum("success", "error", name="crawl_status"), nullable=False
    )
    error_message = Column(Text, nullable=True)
    http_status_code = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    town = relationship("Town", back_populates="crawls")
    notifications = relationship("Notification", back_populates="crawl")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    crawl_id = Column(Integer, ForeignKey("crawls.id"), nullable=False)
    alert_type = Column(
        Enum(
            "lowest_price",
            "price_drop",
            "discount_active",
            name="alert_type",
        ),
        nullable=False,
    )
    current_price = Column(Numeric(10, 2), nullable=False)
    reference_price = Column(Numeric(10, 2), nullable=True)
    sent = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    crawl = relationship("Crawl", back_populates="notifications")


_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(Config.database_url(), pool_pre_ping=True)
    return _engine


def get_session_factory(engine=None):
    global _session_factory
    if _session_factory is None:
        if engine is None:
            engine = get_engine()
        _session_factory = sessionmaker(bind=engine)
    return _session_factory
