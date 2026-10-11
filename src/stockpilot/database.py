"""Database configuration and session lifecycle."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from stockpilot.models import Base

load_dotenv()


def database_url() -> str:
    """Return the configured database URL, creating local SQLite directories as needed."""
    url = os.getenv("STOCKPILOT_DATABASE_URL", "sqlite:///data/stockpilot.db")
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        database_path = Path(url.removeprefix("sqlite:///"))
        if database_path.parent != Path("."):
            database_path.parent.mkdir(parents=True, exist_ok=True)
    return url


def create_database_engine(url: str | None = None) -> Engine:
    """Create an engine and initialize the small application schema."""
    resolved_url = url or database_url()
    connect_args = {"check_same_thread": False} if resolved_url.startswith("sqlite:") else {}
    engine = create_engine(resolved_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Yield a database session and always close it."""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        yield session
