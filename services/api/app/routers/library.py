"""
Saved strategies, IV history, and the earnings calendar.

These are the endpoints backed by the store, so they work with or without a
database — the response carries `backend` so it is obvious which. Without one,
saved work lives until the process restarts, which is fine for evaluating the
product and not fine for using it.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..db.store import get_store, store_status
from ..providers.base import ProviderError
from ..providers.earnings_sources import reports_earnings
from ..services import earnings_service, vol_service

router = APIRouter(prefix="/api/library", tags=["library"])
earnings_router = APIRouter(prefix="/api/earnings", tags=["earnings"])
vol_router = APIRouter(prefix="/api/volatility", tags=["volatility"])


# --------------------------------------------------------------------------
# Saved strategies
# --------------------------------------------------------------------------

class SaveStrategyRequest(BaseModel):
    symbol: str
    name: str = Field(..., max_length=160)
    template_id: str | None = None
    params: dict = Field(default_factory=dict)
    legs: list[dict] = Field(default_factory=list)
    entry_spot: float | None = None
    notes: str = ""


@router.get("/status")
async def status():
    return store_status()


@router.get("/strategies")
async def list_strategies(symbol: str | None = None):
    rows = await get_store().list_strategies(symbol)
    return {"count": len(rows), "backend": get_store().backend, "strategies": rows}


@router.post("/strategies")
async def save_strategy(req: SaveStrategyRequest):
    if not req.legs and not req.template_id:
        raise HTTPException(422, "a saved strategy needs either legs or a template id")
    row = await get_store().save_strategy(
        symbol=req.symbol, name=req.name, template_id=req.template_id,
        params=req.params, legs=req.legs, entry_spot=req.entry_spot,
        notes=req.notes)
    return {**row, "backend": get_store().backend,
            "persistent": store_status()["persistent"]}


@router.get("/strategies/{strategy_id}")
async def get_strategy(strategy_id: str):
    row = await get_store().get_strategy(strategy_id)
    if not row:
        raise HTTPException(404, "strategy not found")
    return row


@router.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str):
    if not await get_store().delete_strategy(strategy_id):
        raise HTTPException(404, "strategy not found")
    return {"deleted": strategy_id}


# --------------------------------------------------------------------------
# Volatility history
# --------------------------------------------------------------------------

@vol_router.get("/history/{symbol}")
async def iv_history(symbol: str, days: int = Query(365, ge=7, le=1830)):
    return await vol_service.iv_history_series(symbol, days)


@vol_router.post("/snapshot")
async def snapshot(symbols: str = Query(..., description="Comma separated tickers")):
    """
    Record today's ATM implied volatility for each symbol.

    Run this once daily after the close — a cron entry or the scheduler of your
    choice. Each day recorded makes IV rank more meaningful; a year of them
    makes it the number it is supposed to be.
    """
    tickers = [s.strip() for s in symbols.split(",") if s.strip()]
    if not tickers:
        raise HTTPException(422, "at least one symbol is required")
    return await vol_service.snapshot_many(tickers)


# --------------------------------------------------------------------------
# Earnings
# --------------------------------------------------------------------------

@earnings_router.get("/{symbol}")
async def earnings(symbol: str, refresh: bool = False,
                   lookback_quarters: int = Query(8, ge=1, le=40)):
    """
    Calendar, implied move, and historical reactions in one call.

    The implied move is reported two ways: the ATM straddle (covers the whole
    life of the option, event included) and the two-expiration decomposition
    that isolates the event itself. They answer different questions and both
    are useful.
    """
    # An index or ETF having no earnings is a fact about the instrument, not a
    # failure to fetch. Returning 502 here made the UI show a data-outage banner
    # on SPY, which is both wrong and alarming. 200 with the fact stated.
    if not reports_earnings(symbol):
        return {
            "symbol": symbol.upper(),
            "reports_earnings": False,
            "next_event": None,
            "following_event": None,
            "implied_move": None,
            "history": {"count": 0, "moves": []},
            "events": [],
            "note": (f"{symbol.upper()} is an index or ETF. It has no earnings "
                     f"announcements, so there is no event to trade around. "
                     f"Its options still have a term structure — see the "
                     f"volatility endpoint."),
        }
    try:
        report = await earnings_service.earnings_report(
            symbol, refresh=refresh, lookback_quarters=lookback_quarters)
    except ProviderError as exc:
        raise HTTPException(503 if exc.provider == "configuration" else 502, str(exc)) from exc
    report.setdefault("reports_earnings", True)
    return report


@earnings_router.get("/{symbol}/in-window")
async def in_window(symbol: str, dte: float = Query(45, ge=0, le=1095)):
    """Does an announcement fall between today and this expiry?"""
    events = await earnings_service.events_in_window(symbol, dte)
    return {
        "symbol": symbol.upper(),
        "dte": dte,
        "expiry": (dt.date.today() + dt.timedelta(days=int(dte))).isoformat(),
        "count": len(events),
        "events": [e.to_dict() for e in events],
    }
