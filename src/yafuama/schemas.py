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
    amazon_condition: str = "used_very_good"
    amazon_listing_status: str | None = None
    amazon_price: int | None = None
    estimated_win_price: int = 0
    shipping_cost: int = 0
    amazon_margin_pct: float = 15.0
    amazon_lead_time_days: int = 4
    amazon_shipping_pattern: str = "2_3_days"
    amazon_condition_note: str = ""
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
    shipping_cost: int | None = None  # None = unknown, 0 = free


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

class ServiceStatus(BaseModel):
    name: str
    status: str  # "ok" / "degraded" / "unavailable"
    detail: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"  # "ok" / "degraded"
    scheduler_running: bool = False
    monitored_count: int = 0
    active_count: int = 0
    services: list[ServiceStatus] = []


class SchedulerAction(BaseModel):
    action: str  # pause / resume


# --- Amazon ---

VALID_CONDITIONS = (
    "used_like_new",     # ほぼ新品
    "used_very_good",    # 非常に良い
    "used_good",         # 良い
    "used_acceptable",   # 可
)


class AmazonListingCreate(BaseModel):
    auction_id: str
    asin: str | None = None
    sku: str | None = None
    condition: str = "used_very_good"  # used_like_new / used_very_good / used_good / used_acceptable
    estimated_win_price: int = 0
    shipping_cost: int = 0
    margin_pct: float | None = None
    lead_time_days: int = 4  # Amazon lead_time_to_ship_max_days (days)
    shipping_pattern: str = "2_3_days"  # "1_2_days" / "2_3_days" / "3_7_days"
    image_urls: list[str] = []  # Selected Yahoo auction image URLs
    condition_note: str = ""  # コンディション説明（中古品向け）


class AmazonListingUpdate(BaseModel):
    estimated_win_price: int | None = None
    shipping_cost: int | None = None
    margin_pct: float | None = None
    amazon_price: int | None = None
    condition: str | None = None
    lead_time_days: int | None = None


class AmazonListingResponse(BaseModel):
    auction_id: str
    amazon_asin: str | None
    amazon_sku: str | None
    amazon_condition: str
    amazon_listing_status: str | None
    amazon_price: int | None
    estimated_win_price: int
    shipping_cost: int
    amazon_margin_pct: float
    amazon_lead_time_days: int = 4
    amazon_shipping_pattern: str = "2_3_days"
    amazon_condition_note: str = ""
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


class ListingRestrictionReason(BaseModel):
    reason_code: str = ""
    message: str = ""


class ListingRestriction(BaseModel):
    condition_type: str = ""
    is_restricted: bool = True
    reasons: list[ListingRestrictionReason] = []


class ListingRestrictionsResponse(BaseModel):
    asin: str
    is_listable: bool = True
    restrictions: list[ListingRestriction] = []


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


# --- Watched Keywords ---

class WatchedKeywordCreate(BaseModel):
    keyword: str
    is_active: bool = True
    notes: str = ""


class WatchedKeywordUpdate(BaseModel):
    is_active: bool | None = None
    notes: str | None = None


class WatchedKeywordResponse(BaseModel):
    id: int
    keyword: str
    is_active: bool
    last_scanned_at: datetime | None
    created_at: datetime
    updated_at: datetime
    notes: str
    alert_count: int = 0
    # AI Discovery fields
    source: str = "manual"
    parent_keyword_id: int | None = None
    performance_score: float = 0.0
    total_scans: int = 0
    total_deals_found: int = 0
    confidence: float = 1.0
    auto_deactivated_at: datetime | None = None

    model_config = {"from_attributes": True}


class WatchedKeywordListResponse(BaseModel):
    keywords: list[WatchedKeywordResponse]
    total: int


class DealAlertResponse(BaseModel):
    id: int
    keyword_id: int
    yahoo_auction_id: str
    amazon_asin: str
    yahoo_title: str
    yahoo_url: str
    yahoo_price: int
    sell_price: int
    gross_profit: int
    gross_margin_pct: float
    notified_at: datetime

    model_config = {"from_attributes": True}


class DealAlertListResponse(BaseModel):
    alerts: list[DealAlertResponse]
    total: int


# --- AI Discovery ---

class KeywordCandidateResponse(BaseModel):
    id: int
    keyword: str
    strategy: str
    confidence: float
    parent_keyword_id: int | None
    reasoning: str
    status: str
    validation_result: str
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class KeywordCandidateListResponse(BaseModel):
    candidates: list[KeywordCandidateResponse]
    total: int


class DiscoveryLogResponse(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    candidates_generated: int
    candidates_validated: int
    keywords_added: int
    keywords_deactivated: int
    keepa_tokens_used: int
    strategy_breakdown: str

    model_config = {"from_attributes": True}


class DiscoveryStatusResponse(BaseModel):
    enabled: bool
    last_cycle: DiscoveryLogResponse | None = None
    total_ai_keywords: int = 0
    active_ai_keywords: int = 0
    pending_candidates: int = 0


class DiscoveryCycleResponse(BaseModel):
    candidates_generated: int = 0
    candidates_validated: int = 0
    keywords_added: int = 0
    keywords_deactivated: int = 0
    keepa_tokens_used: int = 0


class DiscoveryInsightsResponse(BaseModel):
    top_brands: list[dict] = []
    top_product_types: list[dict] = []
    price_ranges: list[dict] = []
    keyword_count: int = 0
    deal_count: int = 0
