from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    host: str = "127.0.0.1"
    port: int = 8001

    database_url: str = "sqlite:///./yafuama.db"

    # Scraper
    scraper_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    scraper_request_timeout: int = 30
    scraper_use_selenium_fallback: bool = False

    # Monitor
    default_check_interval: int = 300
    min_check_interval: int = 30

    # Webhook
    webhook_url: str = ""
    webhook_type: str = "discord"  # discord / slack / line

    # Amazon SP-API
    sp_api_refresh_token: str = ""
    sp_api_lwa_app_id: str = ""
    sp_api_lwa_client_secret: str = ""
    sp_api_aws_access_key: str = ""
    sp_api_aws_secret_key: str = ""
    sp_api_role_arn: str = ""
    sp_api_seller_id: str = ""
    sp_api_marketplace: str = "A1VC38T7YXB528"  # Japan
    sp_api_default_margin_pct: float = 15.0
    sp_api_default_shipping_cost: int = 800

    # Amazon配送テンプレート名（Seller Centralで登録済み）
    shipping_template_1_2_days: str = "1～2の場合"
    shipping_template_2_3_days: str = "2～3の場合"
    shipping_template_3_7_days: str = "3～7の場合"

    @property
    def sp_api_enabled(self) -> bool:
        return bool(self.sp_api_refresh_token and self.sp_api_lwa_app_id)

    # Keepa Pro API
    keepa_api_key: str = ""
    keepa_default_stats_days: int = 90
    keepa_good_rank_threshold: int = 100_000

    # Deal Finder
    deal_forwarding_cost: int = 960       # サイズ不明時のフォールバック（100サイズ相当）
    deal_inspection_fee: int = 0          # 廃止（システム利用料に含まれる）
    deal_system_fee: int = 100            # 無在庫1配送毎のシステム利用料 (円)
    deal_amazon_fee_pct: float = 10.0     # Amazon販売手数料率 (%)
    deal_min_gross_margin_pct: float = 40.0  # 最低粗利率 (%)
    deal_max_gross_margin_pct: float = 70.0  # 粗利率上限 (%) — 超過はほぼ誤マッチ
    deal_strict_margin_pct: float = 50.0   # この粗利率以上はマッチング厳格化
    deal_min_gross_profit: int = 3000     # 最低粗利益 (円)
    deal_scan_interval: int = 600         # 自動スキャン間隔 (秒、デフォルト10分)
    deal_default_shipping: int = 700        # 送料不明時のデフォルト送料 (円)
    deal_scan_max_pages: int = 3           # Yahoo検索の最大ページ数
    deal_max_keepa_searches_per_keyword: int = 10  # キーワードあたりの最大Keepa個別検索数
    deal_min_price_for_keepa_search: int = 2000    # 個別Keepa検索の最低即決価格（円）
    # 深層検証（利益率50%超の候補をヤフオク説明文で再検証）
    deal_deep_validation_enabled: bool = True
    deal_deep_validation_max_per_cycle: int = 10
    deal_deep_validation_margin_threshold: float = 50.0

    @property
    def keepa_enabled(self) -> bool:
        return bool(self.keepa_api_key)

    # AI Discovery
    discovery_enabled: bool = True
    discovery_interval: int = 3600           # Discovery cycle interval (seconds, default 1h)
    discovery_token_budget: int = 10         # Max Keepa tokens per discovery cycle
    discovery_min_deals: int = 5             # Min DealAlerts before AI starts generating
    discovery_auto_add_threshold: float = 0.6  # Confidence threshold for auto-adding
    discovery_max_ai_keywords: int = 50      # Cap on active AI keywords
    discovery_deactivation_scans: int = 10   # Scans before deactivation check
    discovery_deactivation_threshold: float = 0.05  # Score below which to deactivate
    anthropic_api_key: str = ""              # Claude API key (optional)

    # Series Expansion（型番シリーズ横展開）
    series_expansion_min_profit: int = 3000       # シリーズ展開トリガーの最低粗利益（円）
    series_expansion_max_siblings: int = 4        # 1型番あたりの最大兄弟生成数
    series_expansion_max_per_cycle: int = 10      # Discoveryサイクルあたりの最大生成数

    # Demand Discovery（需要ベースキーワード発見）
    demand_finder_enabled: bool = True
    demand_finder_min_drops30: int = 5        # 月間最低販売回数
    demand_finder_min_used_price: int = 10000 # 中古最低価格（円）
    demand_finder_max_results: int = 50       # Product Finder最大取得件数

    @property
    def anthropic_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    # Auth
    api_key: str = ""  # Set to enable API key auth; empty = no auth

    # Log
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
