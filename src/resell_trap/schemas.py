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
    # Amazon integration
    amazon_asin: str | None = None
    amazon_sku: str | None = None
    amazon_listing_status: str | None = None
    amazon_price: int | None = None
    estimated_win_price: int = 0
    shipping_cost: int = 0
    amazon_margin_pct: float = 15.0
    amazon_last_synced_at: datetime | None = None

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


# --- Amazon ---

class AmazonListingCreate(BaseModel):
    auction_id: str
    asin: str | None = None
    sku: str | None = None
    estimated_win_price: int = 0
    shipping_cost: int = 0
    margin_pct: float | None = None


class AmazonListingUpdate(BaseModel):
    estimated_win_price: int | None = None
    shipping_cost: int | None = None
    margin_pct: float | None = None
    amazon_price: int | None = None


class AmazonListingResponse(BaseModel):
    auction_id: str
    amazon_asin: str | None
    amazon_sku: str | None
    amazon_listing_status: str | None
    amazon_price: int | None
    estimated_win_price: int
    shipping_cost: int
    amazon_margin_pct: float
    amazon_last_synced_at: datetime | None

    model_config = {"from_attributes": True}


class CatalogSearchResult(BaseModel):
    asin: str
    title: str = ""
    image_url: str = ""
    brand: str = ""


class CatalogSearchResponse(BaseModel):
    keywords: str
    items: list[CatalogSearchResult]


# --- Keepa ---

class SalesRankAnalysisResponse(BaseModel):
    current_rank: int | None
    avg_rank_30d: int | None
    avg_rank_90d: int | None
    min_rank_90d: int | None
    max_rank_90d: int | None
    rank_trend: str
    sells_well: bool
    rank_threshold_used: int


class UsedPriceAnalysisResponse(BaseModel):
    current_price: int | None
    avg_price_30d: int | None
    avg_price_90d: int | None
    min_price_90d: int | None
    max_price_90d: int | None
    price_trend: str
    price_volatility: float


class PriceRecommendationResponse(BaseModel):
    recommended_price: int
    strategy: str
    reasoning: str
    confidence: str
    market_price_avg: int | None
    market_price_min: int | None


class KeepaAnalysisResponse(BaseModel):
    asin: str
    title: str = ""
    sales_rank: SalesRankAnalysisResponse
    used_price: UsedPriceAnalysisResponse
    recommendation: PriceRecommendationResponse | None = None


class KeepaAnalysisRequest(BaseModel):
    asin: str
    cost_price: int = 0
    shipping_cost: int = 0
    margin_pct: float | None = None
    good_rank_threshold: int | None = None
