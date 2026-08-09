"""
Persistence tests.

Every case runs twice -- once against the in-memory store and once against a
real SQLite database through the same SQLAlchemy layer that serves Postgres.
That is the point: the fallback exists so the product works without a database,
and it is only useful if it behaves identically to the real thing. A fallback
that silently accepts data the real backend would reject is worse than none.
"""

import datetime as dt

import pytest
import pytest_asyncio

from app.db.engine import Database
from app.db.store import MemoryStore, SqlStore
from app.services.vol_service import atm_iv_from_chain

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    if request.param == "memory":
        yield MemoryStore()
        return

    db = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    ok = await db.connect()
    assert ok, f"sqlite backend failed to start: {db.reason}"
    yield SqlStore(db.session_factory)
    await db.dispose()


# --------------------------------------------------------------------------
# Saved strategies
# --------------------------------------------------------------------------

async def test_strategy_round_trip(store):
    saved = await store.save_strategy(
        symbol="nvda", name="45d 16Δ condor", template_id="iron_condor",
        params={"dte": 45, "delta": 0.16}, entry_spot=172.0,
        legs=[{"kind": "put", "quantity": -1, "strike": 150.0, "dte": 45}],
        notes="paper only")

    assert saved["symbol"] == "NVDA"           # normalized on write
    fetched = await store.get_strategy(saved["id"])
    assert fetched is not None
    assert fetched["name"] == "45d 16Δ condor"
    assert fetched["params"]["delta"] == 0.16
    assert fetched["legs"][0]["strike"] == 150.0
    assert fetched["entry_spot"] == 172.0


async def test_strategy_listing_filters_by_symbol(store):
    await store.save_strategy(symbol="NVDA", name="a", legs=[{"x": 1}])
    await store.save_strategy(symbol="SPY", name="b", legs=[{"x": 2}])
    await store.save_strategy(symbol="NVDA", name="c", legs=[{"x": 3}])

    assert len(await store.list_strategies()) == 3
    assert len(await store.list_strategies("NVDA")) == 2
    assert len(await store.list_strategies("spy")) == 1     # case insensitive


async def test_strategy_delete(store):
    saved = await store.save_strategy(symbol="AAPL", name="x", legs=[{}])
    assert await store.delete_strategy(saved["id"]) is True
    assert await store.get_strategy(saved["id"]) is None
    assert await store.delete_strategy(saved["id"]) is False
    assert await store.delete_strategy("does-not-exist") is False


# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------

async def test_position_lifecycle(store):
    pos = await store.add_position(
        symbol="tsla", name="Aug strangle",
        legs=[{"kind": "call", "quantity": -1, "strike": 400.0, "dte": 40,
               "entry_price": 8.5}],
        entry_debit=-850.0)

    assert pos["symbol"] == "TSLA"
    assert pos["closed"] is None
    assert len(await store.list_positions(open_only=True)) == 1

    closed = await store.close_position(pos["id"], exit_credit=310.0)
    assert closed["closed"] is not None
    assert closed["exit_credit"] == 310.0

    assert len(await store.list_positions(open_only=True)) == 0
    assert len(await store.list_positions(open_only=False)) == 1


async def test_closing_a_missing_position_returns_none(store):
    assert await store.close_position("nope", 0.0) is None


async def test_marks_are_recorded_in_order(store):
    pos = await store.add_position(symbol="SPY", name="p", legs=[{}])
    for px in (598.0, 601.0, 595.0):
        await store.record_mark(pos["id"], underlying=px, unrealized=px - 598.0)

    marks = await store.position_marks(pos["id"])
    assert len(marks) == 3
    assert [m["underlying"] for m in marks] == [598.0, 601.0, 595.0]


async def test_deleting_a_position_removes_its_marks(store):
    pos = await store.add_position(symbol="SPY", name="p", legs=[{}])
    await store.record_mark(pos["id"], underlying=600.0)
    assert await store.delete_position(pos["id"]) is True
    assert await store.position_marks(pos["id"]) == []


# --------------------------------------------------------------------------
# IV history
# --------------------------------------------------------------------------

async def test_iv_history_round_trip(store):
    today = dt.date.today()
    for i in range(10):
        await store.record_iv("NVDA", 0.40 + i * 0.01,
                              observed_on=today - dt.timedelta(days=i),
                              realized_20d=0.35, spot=170.0, source="test")

    rows = await store.iv_history("NVDA", days=365)
    assert len(rows) == 10
    assert rows == sorted(rows, key=lambda r: r["date"])       # ascending
    assert rows[-1]["atm_iv_30d"] == pytest.approx(0.40)        # today's value


async def test_same_day_observation_overwrites_rather_than_duplicating(store):
    """
    A service restart or a re-run of the snapshot job must not double-weight a
    day -- that would quietly distort every IV rank computed afterwards.
    """
    today = dt.date.today()
    await store.record_iv("SPY", 0.14, observed_on=today, source="first")
    await store.record_iv("SPY", 0.19, observed_on=today, source="second")

    rows = await store.iv_history("SPY", days=30)
    assert len(rows) == 1
    assert rows[0]["atm_iv_30d"] == pytest.approx(0.19)
    assert rows[0]["source"] == "second"


async def test_iv_history_respects_the_window(store):
    today = dt.date.today()
    await store.record_iv("QQQ", 0.20, observed_on=today - dt.timedelta(days=5))
    await store.record_iv("QQQ", 0.30, observed_on=today - dt.timedelta(days=400))

    assert len(await store.iv_history("QQQ", days=30)) == 1
    assert len(await store.iv_history("QQQ", days=500)) == 2


async def test_iv_history_is_per_symbol(store):
    await store.record_iv("AAPL", 0.25)
    await store.record_iv("MSFT", 0.22)
    assert len(await store.iv_history("AAPL")) == 1
    assert len(await store.iv_history("TSLA")) == 0


# --------------------------------------------------------------------------
# Earnings cache
# --------------------------------------------------------------------------

async def test_earnings_upsert_and_query(store):
    rows = [
        {"symbol": "NVDA", "event_date": dt.date(2026, 8, 20), "session": "amc",
         "date_status": "projected", "eps_estimate": 1.25, "source": "test"},
        {"symbol": "NVDA", "event_date": dt.date(2026, 5, 21), "session": "amc",
         "date_status": "confirmed", "eps_actual": 0.98, "source": "test"},
    ]
    assert await store.upsert_earnings(rows) == 2

    all_rows = await store.get_earnings("NVDA")
    assert len(all_rows) == 2
    assert all_rows[0]["event_date"] < all_rows[1]["event_date"]   # ascending

    future = await store.get_earnings("NVDA", since=dt.date(2026, 7, 1))
    assert len(future) == 1
    assert future[0]["date_status"] == "projected"


async def test_earnings_upsert_updates_a_moved_date_in_place(store):
    """
    A projected date being confirmed must update the row, not add a second one
    for the same announcement.
    """
    await store.upsert_earnings([{
        "symbol": "AAPL", "event_date": dt.date(2026, 10, 29),
        "date_status": "projected", "session": "amc", "source": "nasdaq"}])
    await store.upsert_earnings([{
        "symbol": "AAPL", "event_date": dt.date(2026, 10, 29),
        "date_status": "confirmed", "session": "amc", "eps_estimate": 2.10,
        "source": "benzinga"}])

    rows = await store.get_earnings("AAPL")
    assert len(rows) == 1
    assert rows[0]["date_status"] == "confirmed"
    assert rows[0]["eps_estimate"] == 2.10
    assert rows[0]["source"] == "benzinga"


async def test_earnings_accepts_iso_strings_as_well_as_dates(store):
    await store.upsert_earnings([{
        "symbol": "SPY", "event_date": "2026-09-15", "source": "test"}])
    rows = await store.get_earnings("SPY")
    assert rows[0]["event_date"] == "2026-09-15"


# --------------------------------------------------------------------------
# Database bootstrap
# --------------------------------------------------------------------------

async def test_unreachable_database_reports_rather_than_raising():
    """
    The whole product runs without a database. A bad URL has to degrade, not
    take the process down at startup.
    """
    db = Database("postgresql+asyncpg://nobody:nobody@127.0.0.1:1/none")
    assert await db.connect() is False
    assert db.available is False
    assert db.reason and db.reason != "not initialised"
    await db.dispose()


async def test_missing_url_is_handled():
    db = Database("")
    assert await db.connect() is False
    assert "not set" in db.reason


# --------------------------------------------------------------------------
# ATM IV interpolation (feeds the stored series)
# --------------------------------------------------------------------------

async def test_atm_iv_interpolates_between_expirations():
    """
    Constant-maturity interpolation matters: taking the nearest expiry instead
    makes the stored series jump every time an expiration rolls off, which shows
    up later as volatility that was never there.
    """
    from app.providers.synthetic import SyntheticProvider

    chain = await SyntheticProvider().get_option_chain("NVDA", max_expirations=6)
    dtes = sorted({c.dte for c in chain.contracts})
    assert 30 not in dtes, "test needs a target maturity that is not quoted"

    iv30 = atm_iv_from_chain(chain, 30)
    assert iv30 is not None

    below = max(d for d in dtes if d < 30)
    above = min(d for d in dtes if d > 30)
    iv_below = atm_iv_from_chain(chain, below)
    iv_above = atm_iv_from_chain(chain, above)
    assert min(iv_below, iv_above) <= iv30 <= max(iv_below, iv_above)
