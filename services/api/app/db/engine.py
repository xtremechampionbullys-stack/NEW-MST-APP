"""
Database engine and lifecycle.

Design constraint: the product must run with zero setup. So the database is
optional. If DATABASE_URL is unset, unreachable, or its driver is missing, the
API logs the reason once and the store layer transparently uses an in-memory
backend instead. Nothing 500s because Postgres is not running.

Dialect handling:
  * postgresql+asyncpg -- production. Timescale hypertables are applied after
    table creation when the extension is available; if it is not, the tables are
    plain Postgres and everything still works, just without chunking.
  * sqlite+aiosqlite -- local dev and the test suite. No extension needed.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings
from .models import Base

log = logging.getLogger(__name__)

# Tables worth converting to Timescale hypertables. Everything else is small
# and relational; chunking it would add overhead for no benefit.
HYPERTABLES = [("position_marks", "ts")]


class Database:
    def __init__(self, url: str | None = None):
        # None means "use the configured default"; an explicit empty string
        # means "no database". Collapsing the two with `or` made it impossible
        # for a caller to opt out of persistence.
        self.url = get_settings().database_url if url is None else url
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.available = False
        self.reason = "not initialised"

    @property
    def dialect(self) -> str:
        return self.url.split(":", 1)[0].split("+", 1)[0] if self.url else ""

    async def connect(self) -> bool:
        if not self.url:
            self.reason = "DATABASE_URL is not set"
            return False
        try:
            self.engine = create_async_engine(
                self.url,
                echo=False,
                pool_pre_ping=True,
                # SQLite has no real pool and rejects these arguments.
                **({} if self.dialect == "sqlite" else {"pool_size": 5, "max_overflow": 5}),
            )
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await self._ensure_position_mark_closing(conn)
                if self.dialect == "postgresql":
                    await self._apply_hypertables(conn)
            self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
            self.available = True
            self.reason = "connected"
            log.info("database connected (%s)", self.dialect)
            return True
        except Exception as exc:                                # noqa: BLE001
            # A missing driver, a refused connection, and bad credentials all
            # land here and all mean the same thing to the caller: no database.
            self.available = False
            self.reason = f"{type(exc).__name__}: {exc}"
            log.warning("database unavailable, using in-memory store (%s)", self.reason)
            if self.engine is not None:
                try:
                    await self.engine.dispose()
                except Exception:                               # noqa: BLE001
                    pass
                self.engine = None
            return False

    async def _ensure_position_mark_closing(self, conn) -> None:
        """Add the ``closing`` flag to ``position_marks`` on older databases.

        ``create_all`` only creates missing tables; it never alters existing
        ones, so a database created before the closing flag existed lacks the
        column and a paper close would silently fail to persist its final mark
        (the write is wrapped in a try/except in the route). This idempotent
        migration brings those databases up to date. Runs on every connect and
        is a no-op once the column exists.
        """
        from sqlalchemy import text
        try:
            if self.dialect == "sqlite":
                cols = await conn.execute(
                    text("PRAGMA table_info(position_marks)"))
                has = any(row[1] == "closing" for row in cols)
            else:
                has = (await conn.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='position_marks' AND column_name='closing'")
                )).scalar_one_or_none() is not None
            if not has:
                await conn.execute(text(
                    "ALTER TABLE position_marks ADD COLUMN closing BOOLEAN"))
        except Exception as exc:                                # noqa: BLE001
            log.info("could not ensure position_marks.closing (%s)", exc)

    async def _apply_hypertables(self, conn) -> None:
        from sqlalchemy import text

        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        except Exception as exc:                                # noqa: BLE001
            log.info("timescaledb extension not available (%s); using plain tables", exc)
            return

        for table, column in HYPERTABLES:
            try:
                await conn.execute(text(
                    f"SELECT create_hypertable('{table}', '{column}', "
                    f"if_not_exists => TRUE, migrate_data => TRUE)"))
            except Exception as exc:                            # noqa: BLE001
                log.info("could not convert %s to a hypertable (%s)", table, exc)

    async def dispose(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
        self.engine = None
        self.session_factory = None
        self.available = False


_db: Database | None = None


def get_database() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


async def init_database(url: str | None = None) -> Database:
    global _db
    if _db is not None:
        await _db.dispose()
    _db = Database(url)
    await _db.connect()
    return _db


async def close_database() -> None:
    global _db
    if _db is not None:
        await _db.dispose()
    _db = None
