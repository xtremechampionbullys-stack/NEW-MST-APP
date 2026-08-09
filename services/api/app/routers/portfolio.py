"""
Portfolio and agents.

The portfolio endpoints run real math on positions held in memory: aggregate
greeks, beta-weighted delta, concentration, and the risk checks that actually
matter in an options book. What is not here yet is persistence (the Postgres
schema is in infra/schema.sql) and broker sync.

The agent registry is a manifest, not a simulator. Each entry declares what the
agent consumes, what it emits, and what it is allowed to do. Nothing here
executes -- an agent that can trade before its risk gate has been tested is
exactly the failure mode this structure exists to prevent.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db.store import get_store, store_status
from ..engine.analytics import analyze
from ..engine.payoff import Leg, Position
from ..providers.base import ProviderError
from ..providers.registry import get_registry

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

# Positions live in the store, which is Postgres when DATABASE_URL points at a
# reachable database and an in-memory dict otherwise. Every response carries the
# backend so it is never ambiguous whether the data will survive a restart.


class PositionLeg(BaseModel):
    kind: Literal["call", "put", "stock"]
    quantity: float
    strike: float | None = None
    dte: float = 0
    entry_price: float
    iv: float = 0.30


class PositionInput(BaseModel):
    symbol: str
    name: str = "Position"
    legs: list[PositionLeg]
    opened: str | None = None
    notes: str = ""


@router.get("/positions")
async def list_positions(open_only: bool = True):
    rows = await get_store().list_positions(open_only=open_only)
    return {"count": len(rows), "backend": get_store().backend,
            "persistent": store_status()["persistent"], "positions": rows}


@router.post("/positions")
async def add_position(req: PositionInput):
    if not req.legs:
        raise HTTPException(422, "a position needs at least one leg")
    row = await get_store().add_position(
        symbol=req.symbol, name=req.name,
        legs=[leg.model_dump() for leg in req.legs],
        opened=req.opened, notes=req.notes)
    return {**row, "backend": get_store().backend}


@router.post("/positions/{position_id}/close")
async def close_position(position_id: str, exit_credit: float | None = None):
    """
    Close a position, valuing the exit if the caller does not.

    The old behaviour recorded `exit_credit: null` when none was supplied — a
    close that never says what the position was worth, which makes every
    realized-P&L question afterwards unanswerable. Now the exit is marked at
    current quotes with the same repricing the risk endpoint uses, and the
    response says which method produced the number. A paper exit fills at the
    theoretical mark: no queue, no slippage, no fees — stated, because that is
    precisely how paper results flatter themselves relative to live ones.
    """
    store = get_store()
    open_rows = await store.list_positions(open_only=True)
    rec = next((r for r in open_rows if r["id"] == position_id), None)
    if not rec:
        raise HTTPException(404, "position not found (or already closed)")

    legs = [Leg(kind=l["kind"], quantity=l["quantity"],
                entry_price=l["entry_price"], strike=l.get("strike"),
                dte=l.get("dte", 0), iv=l.get("iv", 0.30))
            for l in rec["legs"]]

    method = "caller-supplied exit value"
    realized = None
    try:
        quote = await get_registry().quote(rec["symbol"])
        position = Position(legs=legs, spot=quote.last, name=rec["name"])
        if exit_credit is None:
            unrealized = position.pl_at(quote.last, 0.0)
            exit_credit = unrealized + position.net_debit
            method = ("theoretical mark at the current quote -- a paper exit, "
                      "with no queue, slippage or fees")
        realized = round(exit_credit - position.net_debit, 2)
        # One final mark, so the recorded series ends where the position did.
        try:
            await store.record_mark(position_id, underlying=quote.last,
                                    mark_value=exit_credit,
                                    unrealized=realized, closing=True)
        except Exception:                                          # noqa: BLE001
            pass
    except ProviderError as exc:
        if exit_credit is None:
            raise HTTPException(
                502, f"Cannot value the exit: no quote for {rec['symbol']} "
                     f"({exc}). Pass exit_credit explicitly to close anyway.") from exc

    row = await store.close_position(position_id, round(exit_credit, 2))
    return {**row, "realized": realized, "exit_method": method}


@router.get("/positions/{position_id}/marks")
async def position_marks(position_id: str, limit: int = 400):
    """
    The recorded mark history for one position.

    Marks are measurements, not derivations: reconstructing an equity curve
    after the fact means guessing what implied volatility was on each day, which
    is exactly the assumption that makes a backtest flatter itself.
    """
    return {"position_id": position_id,
            "marks": await get_store().position_marks(position_id, limit)}


@router.delete("/positions/{position_id}")
async def remove_position(position_id: str):
    if not await get_store().delete_position(position_id):
        raise HTTPException(404, "position not found")
    return {"deleted": position_id}


@router.get("/risk")
async def portfolio_risk(beta_benchmark: str = "SPY", record_marks: bool = False):
    """
    Aggregate risk across the book.

    Beta-weighted delta is the number that matters most and the one people
    skip: five separate bullish positions in correlated names are one position,
    and this is where that becomes visible.
    """
    store = get_store()
    stored = await store.list_positions(open_only=True)
    if not stored:
        return {"positions": 0, "message": "No positions tracked yet.",
                "greeks": None, "backend": store.backend}

    registry = get_registry()
    try:
        benchmark = await registry.quote(beta_benchmark)
    except ProviderError:
        benchmark = None

    totals = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    beta_delta = 0.0
    per_symbol: dict[str, dict] = {}
    rows = []

    for record in stored:
        symbol = record["symbol"]
        try:
            quote = await registry.quote(symbol)
        except ProviderError as exc:
            rows.append({"id": record["id"], "symbol": symbol,
                         "error": str(exc)})
            continue

        legs = [Leg(kind=l["kind"], quantity=l["quantity"],
                    entry_price=l["entry_price"],
                    strike=l.get("strike"), dte=l.get("dte", 0),
                    iv=l.get("iv", 0.30))
                for l in record["legs"]]
        position = Position(legs=legs, spot=quote.last, name=record["name"],
                            metadata={"symbol": symbol})
        a = analyze(position)
        g = a["net_greeks"]

        for k in totals:
            totals[k] += g[k]

        # Beta of 1.0 is a placeholder: real betas need a regression against the
        # benchmark's returns, which belongs in a scheduled job rather than a
        # request handler. Labelled so nobody mistakes it for a measurement.
        beta = 1.0
        beta_delta += g["delta"] * beta * (quote.last / (benchmark.last if benchmark else quote.last))

        bucket = per_symbol.setdefault(symbol, {"symbol": symbol, "delta": 0.0,
                                                "vega": 0.0, "theta": 0.0,
                                                "positions": 0})
        bucket["delta"] += g["delta"]
        bucket["vega"] += g["vega"]
        bucket["theta"] += g["theta"]
        bucket["positions"] += 1

        unrealized = position.pl_at(quote.last, 0.0)
        if record_marks:
            # Opt-in: writing a mark on every risk read would pollute the series
            # with duplicates whenever the page refreshes.
            try:
                await store.record_mark(
                    record["id"], underlying=quote.last,
                    mark_value=unrealized + position.net_debit,
                    unrealized=unrealized, delta=g["delta"], gamma=g["gamma"],
                    theta=g["theta"], vega=g["vega"])
            except Exception:                                  # noqa: BLE001
                pass

        rows.append({
            "id": record["id"], "symbol": symbol, "name": record["name"],
            "spot": quote.last,
            "unrealized": unrealized,
            "max_loss": a["max_loss"], "max_loss_unbounded": a["max_loss_unbounded"],
            "probability_of_profit": a["probability_of_profit"],
            "dte": a["dte"], "greeks": g, "risk_defined": a["risk_defined"],
        })

    warnings = []
    undefined = [r for r in rows if r.get("max_loss_unbounded")]
    if undefined:
        warnings.append(
            f"{len(undefined)} position(s) carry unlimited upside risk.")
    if totals["vega"] < 0 and abs(totals["vega"]) > 200:
        warnings.append(
            f"Net vega is {totals['vega']:,.0f}. A broad volatility expansion "
            f"hits the whole book at once, not one position at a time.")
    concentrated = [s for s in per_symbol.values()
                    if abs(s["delta"]) > 0.4 * abs(totals["delta"] or 1)]
    if len(per_symbol) > 1 and concentrated:
        warnings.append(
            "Directional exposure is concentrated in "
            + ", ".join(s["symbol"] for s in concentrated) + ".")

    return {
        "positions": len(rows),
        "backend": store.backend,
        "persistent": store_status()["persistent"],
        "greeks": totals,
        "beta_weighted_delta": round(beta_delta, 2),
        "beta_benchmark": beta_benchmark.upper(),
        "beta_note": ("Beta is assumed 1.0 until a returns regression job is "
                      "wired in -- treat the beta-weighted figure as indicative."),
        "by_symbol": list(per_symbol.values()),
        "detail": rows,
        "warnings": warnings,
    }


class PaperOrder(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(ge=1, le=100000)


@router.post("/paper-order")
async def paper_order(req: PaperOrder):
    """
    The terminal's paper ticket: an equity order filled into the paper book.

    The fill is the current mid when both sides of the quote exist, else last —
    fetched server-side at execution, not trusted from the client, because a
    client-supplied "fill" is a backdated trade waiting to happen. The response
    names the method. What a paper fill never has: a queue, partial fills,
    slippage or fees. Those absences are why paper P&L flatters, and the note
    travels with the position instead of being small print somewhere else.

    Options structures are deliberately not squeezed into this ticket — the
    Strategy Lab builds those with the full chain and greeks in view, and a
    four-leg condor typed blind into qty/side boxes is how paper accounts teach
    bad habits.
    """
    try:
        quote = await get_registry().quote(req.symbol)
    except ProviderError as exc:
        raise HTTPException(502, f"No quote for {req.symbol}: {exc}") from exc

    both_sided = quote.bid is not None and quote.ask is not None and quote.ask >= quote.bid > 0
    fill = quote.mid if both_sided else quote.last
    method = "mid of the live quote" if both_sided else "last trade (no two-sided quote)"

    qty = req.quantity if req.side == "BUY" else -req.quantity
    store = get_store()
    row = await store.add_position(
        symbol=req.symbol, name=f"{req.side} {req.quantity} {req.symbol.upper()}",
        legs=[{"kind": "stock", "quantity": qty, "entry_price": round(fill, 4)}],
        notes=f"paper ticket fill at {method}")
    try:
        await store.record_mark(row["id"], underlying=quote.last,
                                mark_value=0.0, unrealized=0.0, entry=True)
    except Exception:                                              # noqa: BLE001
        pass

    return {
        "position": row,
        "fill": {"price": round(fill, 4), "method": method,
                 "source": quote.source, "delayed": quote.delayed},
        "note": ("Paper fill: no queue, no partial fills, no slippage, no fees. "
                 "Live executions have all four, so expect live results to be "
                 "worse than paper ones at the same decisions."),
    }


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------

agents_router = APIRouter(prefix="/api/agents", tags=["agents"])

AGENT_MANIFEST = [
    {
        "id": "vol-regime",
        "name": "Volatility Regime Classifier",
        "status": "specified",
        "purpose": ("Classify each watchlist name into a volatility regime from "
                    "IV rank, term structure slope, and the implied-realized "
                    "spread, then gate which strategy families are eligible."),
        "consumes": ["/api/market/volatility/{symbol}"],
        "emits": ["regime", "eligible_strategy_categories"],
        "permissions": ["read_market_data"],
    },
    {
        "id": "premium-scanner",
        "name": "Premium Selling Scanner",
        "status": "specified",
        "purpose": ("Rank the watchlist for short-premium setups: IV rank above "
                    "threshold, adequate liquidity, credit above a third of "
                    "spread width, no earnings inside the expiry."),
        "consumes": ["/api/market/chain/{symbol}", "/api/strategy/analyze"],
        "emits": ["ranked_candidates"],
        "permissions": ["read_market_data"],
    },
    {
        "id": "position-manager",
        "name": "Position Management Agent",
        "status": "specified",
        "purpose": ("Watch open positions against management rules -- 50% of "
                    "credit, 21 DTE, short strike tested -- and raise an "
                    "actionable alert. Proposes, never executes."),
        "consumes": ["/api/portfolio/risk"],
        "emits": ["alerts", "proposed_adjustments"],
        "permissions": ["read_portfolio", "notify"],
    },
    {
        "id": "risk-gate",
        "name": "Frozen Risk Gate",
        "status": "implemented",   # /api/algo/gate -- every gateway order passes through it
        "purpose": ("Hard limits on per-trade and daily loss that no other agent "
                    "can raise. Any order-generating path must pass through it. "
                    "Deliberately the least clever component in the system."),
        "consumes": ["proposed_orders"],
        "emits": ["approved_orders", "rejections"],
        "permissions": ["veto"],
        "endpoint": "/api/algo/gate",
    },
    {
        "id": "earnings-watch",
        "name": "Earnings Calendar Watch",
        "status": "specified",
        "purpose": ("Flag positions and candidates with an earnings date inside "
                    "the expiry window -- the single most common way a "
                    "well-constructed premium trade goes wrong."),
        "consumes": ["earnings_calendar"],
        "emits": ["event_flags"],
        "permissions": ["read_market_data", "notify"],
    },
]


@agents_router.get("")
async def list_agents():
    return {
        "count": len(AGENT_MANIFEST),
        "agents": AGENT_MANIFEST,
        "note": ("These are declared interfaces, not running processes. Each is "
                 "specified so it can be implemented and tested independently; "
                 "none can place an order without passing the risk gate."),
    }


@agents_router.get("/{agent_id}")
async def agent_detail(agent_id: str):
    for agent in AGENT_MANIFEST:
        if agent["id"] == agent_id:
            return agent
    raise HTTPException(404, f"unknown agent: {agent_id}")
