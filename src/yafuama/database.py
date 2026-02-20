from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations() -> None:
    """Run Alembic migrations to bring the database up to date."""
    project_root = Path(__file__).resolve().parents[2]
    alembic_cfg = Config(str(project_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(project_root / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_cfg, "head")


def init_db() -> None:
    """Legacy: create tables directly (used by tests)."""
    Base.metadata.create_all(bind=engine)
