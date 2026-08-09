"""Test-suite environment.

The API is designed to run with no database or network (see README): the
persistence layer transparently falls back to memory, and the documented
zero-setup backend is SQLite (``DATABASE_URL=sqlite+aiosqlite:///...``).

Without this fixture the default ``DATABASE_URL`` points at a Postgres server
that is not running during CI, and every TestClient startup waits on the
asyncpg connect timeout before falling back to memory — turning a 3-second
suite into a ~20-minute one. Setting the SQLite backend here makes the whole
suite fast and deterministic with no external services.

The persistence tests exercise the memory and Postgres paths explicitly and are
unaffected by this default.
"""

import os

# Must be set before ``app.main`` is imported anywhere, so this lives at the
# top of the test-suite conftest (imported before any test module).
os.environ.setdefault(
    "DATABASE_URL", "sqlite+aiosqlite:///./_test_marketshark.db"
)
# Contract tests deliberately exercise the deterministic demo fixture. The
# running application uses MARKET_DATA_MODE=live and must not use it.
os.environ.setdefault("MARKET_DATA_MODE", "demo")
