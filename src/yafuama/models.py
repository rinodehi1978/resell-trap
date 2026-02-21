from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint
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

    # Amazon integration
    amazon_asin: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    amazon_sku: Mapped[str | None] = mapped_column(Text, nullable=True)
    amazon_condition: Mapped[str] = mapped_column(Text, default="used_very_good")  # used_like_new / used_very_good / used_good / used_acceptable
    amazon_listing_status: Mapped[str | None] = mapped_column(Text, nullable=True)  # active / inactive / error
    amazon_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_win_price: Mapped[int] = mapped_column(Integer, default=0)
    shipping_cost: Mapped[int] = mapped_column(Integer, default=0)  # Yahoo送料
    forwarding_cost: Mapped[int] = mapped_column(Integer, default=0)  # 転送費
    amazon_fee_pct: Mapped[float] = mapped_column(Float, default=10.0)  # Amazon販売手数料率
    amazon_margin_pct: Mapped[float] = mapped_column(Float, default=15.0)
    amazon_lead_time_days: Mapped[int] = mapped_column(Integer, default=4)  # lead_time_to_ship_max_days
    amazon_shipping_pattern: Mapped[str] = mapped_column(Text, default="2_3_days")
    amazon_condition_note: Mapped[str] = mapped_column(Text, default="")  # ユーザー編集済みコンディション説明
    amazon_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seller_central_checklist: Mapped[str] = mapped_column(Text, default="")  # JSON: {"lead_time":false,"images":false,"condition":false}

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


class WatchedKeyword(Base):
    __tablename__ = "watched_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(Text, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    notes: Mapped[str] = mapped_column(Text, default="")

    # AI Discovery fields
    source: Mapped[str] = mapped_column(Text, default="manual")
    # "manual" | "ai_brand" | "ai_title" | "ai_category" | "ai_synonym" | "ai_llm"
    parent_keyword_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("watched_keywords.id"), nullable=True
    )
    performance_score: Mapped[float] = mapped_column(Float, default=0.0)
    total_scans: Mapped[int] = mapped_column(Integer, default=0)
    total_deals_found: Mapped[int] = mapped_column(Integer, default=0)
    total_gross_profit: Mapped[int] = mapped_column(Integer, default=0)
    scans_since_last_deal: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    auto_deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    alerts: Mapped[list["DealAlert"]] = relationship(back_populates="keyword", cascade="all, delete-orphan")


class DealAlert(Base):
    __tablename__ = "deal_alerts"
    __table_args__ = (
        UniqueConstraint("yahoo_auction_id", "amazon_asin", name="uq_deal_alert"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword_id: Mapped[int] = mapped_column(Integer, ForeignKey("watched_keywords.id"))
    yahoo_auction_id: Mapped[str] = mapped_column(Text, index=True)
    amazon_asin: Mapped[str] = mapped_column(Text)
    yahoo_title: Mapped[str] = mapped_column(Text, default="")
    yahoo_url: Mapped[str] = mapped_column(Text, default="")
    yahoo_image_url: Mapped[str] = mapped_column(Text, default="")
    amazon_title: Mapped[str] = mapped_column(Text, default="")
    yahoo_price: Mapped[int] = mapped_column(Integer, default=0)
    yahoo_shipping: Mapped[int] = mapped_column(Integer, default=0)
    sell_price: Mapped[int] = mapped_column(Integer, default=0)
    gross_profit: Mapped[int] = mapped_column(Integer, default=0)
    gross_margin_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Cost breakdown for profit recalculation
    amazon_fee_pct: Mapped[float] = mapped_column(Float, default=10.0)
    forwarding_cost: Mapped[int] = mapped_column(Integer, default=0)
    notified_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # Rejection feedback
    status: Mapped[str] = mapped_column(Text, default="active")  # active / rejected
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    # "wrong_product" | "accessory" | "model_variant" | "bad_price" | "other"
    rejection_note: Mapped[str] = mapped_column(Text, default="")
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    keyword: Mapped["WatchedKeyword"] = relationship(back_populates="alerts")


class KeywordCandidate(Base):
    __tablename__ = "keyword_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    keyword: Mapped[str] = mapped_column(Text, index=True)
    strategy: Mapped[str] = mapped_column(Text, default="")
    # "brand" | "title" | "category" | "synonym" | "llm"
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    parent_keyword_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("watched_keywords.id"), nullable=True
    )
    reasoning: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="pending")
    # "pending" | "validated" | "auto_added" | "approved" | "rejected"
    validation_result: Mapped[str] = mapped_column(Text, default="")
    # JSON: {"yahoo_count": 15, "keepa_count": 3, "best_margin": 52.0, "best_profit": 4500}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DiscoveryLog(Base):
    __tablename__ = "discovery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="running")
    # "running" | "completed" | "error"
    candidates_generated: Mapped[int] = mapped_column(Integer, default=0)
    candidates_validated: Mapped[int] = mapped_column(Integer, default=0)
    keywords_added: Mapped[int] = mapped_column(Integer, default=0)
    keywords_deactivated: Mapped[int] = mapped_column(Integer, default=0)
    keepa_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    strategy_breakdown: Mapped[str] = mapped_column(Text, default="{}")
    # JSON: {"brand": 3, "title": 5, "category": 2}
    error_message: Mapped[str] = mapped_column(Text, default="")


class ConditionTemplate(Base):
    __tablename__ = "condition_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    condition_type: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class ListingPreset(Base):
    __tablename__ = "listing_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(Text, index=True)
    condition: Mapped[str] = mapped_column(Text)
    condition_note: Mapped[str] = mapped_column(Text, default="")
    shipping_pattern: Mapped[str] = mapped_column(Text, default="2_3_days")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RejectionPattern(Base):
    __tablename__ = "rejection_patterns"
    __table_args__ = (
        UniqueConstraint("pattern_type", "pattern_key", name="uq_rejection_pattern"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern_type: Mapped[str] = mapped_column(Text, index=True)
    # "accessory_word" | "problem_pair" | "model_conflict" | "blocked_asin" | "threshold_hint"
    pattern_key: Mapped[str] = mapped_column(Text, default="")
    pattern_data: Mapped[str] = mapped_column(Text, default="{}")
    hit_count: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
