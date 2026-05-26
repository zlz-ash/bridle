"""Database engine and session factory — workspace-anchored."""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bridle.config import get_config


def _create_engine():
    config = get_config()
    engine = create_async_engine(config.database_url, echo=False)
    configure_sqlite_engine(engine)
    return engine


def configure_sqlite_engine(engine: AsyncEngine) -> AsyncEngine:
    """Configure SQLite for workspace-local environments with limited file locks."""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=MEMORY")
        cursor.close()

    return engine


def _create_session_factory():
    engine = _create_engine()
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# Lazy initialization — created on first use after workspace is set.
_engine = None
async_session = None


def _ensure_engine():
    global _engine, async_session
    if _engine is None:
        _engine, async_session = _create_session_factory()


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a database session."""
    _ensure_engine()
    async with async_session() as session:
        yield session
