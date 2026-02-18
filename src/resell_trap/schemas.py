from datetime import datetime

from pydantic import BaseModel, Field


# --- MonitoredItem ---

class ItemCreate(BaseModel):
    auction_id: str | None = None
    url: str | None = None
    check_interval_seconds: int = 300
    auto_adjust_interval: bool = True
    notes: str = ""


class ItemUpdate(BaseModel):
    check_interval_seconds: int | None = None
    auto_adjust_interval: bool | None = None
    is_monitoring_active: bool | None = None
    notes: str | None = None


class ItemResponse(BaseModel):
    id: int
    auction_id: str
    title: str
    url: str
    image_url: str
    category_id: str
    seller_id: str
    current_price: int
    start_price: int
    buy_now_price: int
    win_price: int
    start_time: datetime | None
    end_time: datetime | None
    bid_count: int
    status: str
    check_interval_seconds: int
    auto_adjust_interval: bool
    is_monitoring_active: bool
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    notes: str

    model_config = {"from_attributes": True}


class ItemListResponse(BaseModel):
    items: list[ItemResponse]
    total: int


# --- StatusHistory ---

class StatusHistoryResponse(BaseModel):
    id: int
    item_id: int
    auction_id: str
    change_type: str
    old_status: str | None
    new_status: str | None
    old_price: int | None
    new_price: int | None
    old_bid_count: int | None
    new_bid_count: int | None
    recorded_at: datetime

    model_config = {"from_attributes": True}


# --- NotificationLog ---

class NotificationLogResponse(BaseModel):
    id: int
    item_id: int
    channel: str
    event_type: str
    message: str
    success: bool
    sent_at: datetime

    model_config = {"from_attributes": True}


# --- Search ---

class SearchResultItem(BaseModel):
    auction_id: str
    title: str
    url: str
    image_url: str
    current_price: int
    buy_now_price: int
    start_price: int
    bid_count: int
    end_time: datetime | None
    seller_id: str
    category_id: str


class SearchResponse(BaseModel):
    query: str
    page: int
    items: list[SearchResultItem]
    total_results: int | None = None


# --- Parsed auction data (internal) ---

class AuctionData(BaseModel):
    auction_id: str
    title: str = ""
    url: str = ""
    image_url: str = ""
    category_id: str = ""
    seller_id: str = ""
    current_price: int = 0
    start_price: int = 0
    buy_now_price: int = 0
    win_price: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None
    bid_count: int = 0
    is_closed: bool = False
    has_winner: bool = False

    @property
    def status(self) -> str:
        if not self.is_closed:
            return "active"
        if self.has_winner:
            return "ended_sold"
        return "ended_no_winner"


# --- System ---

class HealthResponse(BaseModel):
    status: str = "ok"
    scheduler_running: bool = False
    monitored_count: int = 0
    active_count: int = 0


class SchedulerAction(BaseModel):
    action: str  # pause / resume
