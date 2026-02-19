from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./resell_trap.db"

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

    @property
    def sp_api_enabled(self) -> bool:
        return bool(self.sp_api_refresh_token and self.sp_api_lwa_app_id)

    # Log
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
