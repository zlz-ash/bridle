"""API dependency injection."""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

_test_db: AsyncSession | None = None


def set_test_db(session: AsyncSession) -> None:
    """Set the test database session (called by conftest)."""
    global _test_db
    _test_db = session


def is_test_mode() -> bool:
    """True when API runs against the in-memory test session."""
    return _test_db is not None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session. Uses test_db if set, otherwise production."""
    if _test_db is not None:
        yield _test_db
    else:
        from bridle.database import async_session

        async with async_session() as session:
            yield session
