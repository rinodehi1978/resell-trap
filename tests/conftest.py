"""Test fixtures: in-memory DB and sample HTML loading."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from yafuama.database import Base

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def active_html() -> str:
    return (SAMPLES_DIR / "yahoo_auction_active.html").read_text(encoding="utf-8")


@pytest.fixture()
def ended_html() -> str:
    return (SAMPLES_DIR / "yahoo_auction_ended.html").read_text(encoding="utf-8")


@pytest.fixture()
def search_html() -> str:
    return (SAMPLES_DIR / "yahoo_search.html").read_text(encoding="utf-8")
