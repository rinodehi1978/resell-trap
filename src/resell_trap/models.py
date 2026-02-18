from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class MonitoredItem(Base):
    __tablename__ = "monitored_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    auction_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(Text, default="")
    category_id: Mapped[str] = mapped_column(Text, default="")
    seller_id: Mapped[str] = mapped_column(Text, default="")

    current_price: Mapped[int] = mapped_column(Integer, default=0)
    start_price: Mapped[int] = mapped_column(Integer, default=0)
    buy_now_price: Mapped[int] = mapped_column(Integer, default=0)
    win_price: Mapped[int] = mapped_column(Integer, default=0)

    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bid_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(Text, default="active")  # active / ended_no_winner / ended_sold

    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    auto_adjust_interval: Mapped[bool] = mapped_column(Boolean, default=True)
    is_monitoring_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")

    history: Mapped[list["StatusHistory"]] = relationship(back_populates="item", cascade="all, delete-orphan")
    notifications: Mapped[list["NotificationLog"]] = relationship(back_populates="item", cascade="all, delete-orphan")


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("monitored_items.id"))
    auction_id: Mapped[str] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(Text)  # status_change / price_change / bid_change / initial

    old_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    old_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_bid_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_bid_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    item: Mapped["MonitoredItem"] = relationship(back_populates="history")


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey("monitored_items.id"))
    channel: Mapped[str] = mapped_column(Text)  # log / webhook
    event_type: Mapped[str] = mapped_column(Text)  # ended / sold / price_change / error
    message: Mapped[str] = mapped_column(Text, default="")
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    item: Mapped["MonitoredItem"] = relationship(back_populates="notifications")
