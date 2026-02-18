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

    # Log
    log_level: str = "INFO"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
