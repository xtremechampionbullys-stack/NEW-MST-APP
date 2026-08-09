"""
Dashboard aggregation.

One request instead of four per symbol. Beyond saving round trips this matters
for rate limits: Massive's free tier is 5 requests a minute and Webull's option
snapshot is 60, so a client that fanned out quote + chain + volatility +
earnings for an eight-name watchlist would exhaust the budget on a single page
load. Aggregating server-side lets the shared TTL cache absorb it.

Per-symbol failures are contained. One dead ticker returns an `error` field on
its own row rather than failing the page — a watchlist that goes blank because
one name is untradeable is worse than a watchlist with one blank row.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Query

from ..engine.vol import realized_vol
from ..providers.base import ProviderError
from ..providers.registry import get_registry
from ..services import earnings_service, market_map, news_impact
from ..services.vol_service import atm_iv_from_chain, iv_rank_for

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


async def _symbol_row(symbol: str, *, include_earnings: bool,
                      sparkline_points: int) -> dict:
    registry = get_registry()
    row: dict = {"symbol": symbol.upper()}

    try:
        quote = await registry.quote(symbol)
    except ProviderError as exc:
        return {"symbol": symbol.upper(), "error": str(exc)}

    row.update({
        "last": quote.last,
        "change": quote.change,
        "change_pct": quote.change_pct,
        "source": quote.source,
        "synthetic": quote.source == "synthetic",
    })

    try:
        bars = await registry.bars(symbol, limit=max(sparkline_points, 60))
        closes = [b.c for b in bars]
        row["sparkline"] = [round(c, 4) for c in closes[-sparkline_points:]]
        row["realized_vol_20d"] = realized_vol(closes, 20)
    except ProviderError:
        row["sparkline"] = []
        row["realized_vol_20d"] = None

    try:
        chain = await registry.chain(symbol, max_expirations=6)
        atm = atm_iv_from_chain(chain, 30)
        row["atm_iv_30d"] = atm
        if atm:
            pool = [c.implied_vol for c in chain.contracts
                    if c.implied_vol and 20 <= c.dte <= 60]
            rank = await iv_rank_for(symbol, atm, pool)
            row["iv_rank"] = rank.get("iv_rank")
            row["iv_rank_method"] = rank.get("method")
            rv = row.get("realized_vol_20d")
            # Positive means options price more movement than the stock has
            # recently delivered -- the premium seller's edge, when it persists.
            row["variance_risk_premium"] = (round(atm - rv, 5)
                                            if rv is not None else None)
    except ProviderError as exc:
        row["chain_error"] = str(exc)

    if include_earnings:
        try:
            events, _ = await earnings_service.load_events(
                symbol, lookback_quarters=1, lookahead_days=120)
            upcoming = earnings_service.next_event(events)
            if upcoming:
                row["earnings"] = {
                    "date": upcoming.event_date.isoformat(),
                    "days_away": upcoming.days_away,
                    "session": upcoming.session,
                    "confirmed": upcoming.is_confirmed,
                    "synthetic": upcoming.is_synthetic,
                }
        except Exception as exc:                               # noqa: BLE001
            log.debug("earnings unavailable for %s: %s", symbol, exc)

    return row


@router.get("/watchlist")
async def watchlist(
    symbols: str = Query("SPY,QQQ,NVDA,AAPL,TSLA,AMD",
                         description="Comma separated tickers"),
    include_earnings: bool = True,
    sparkline_points: int = Query(40, ge=10, le=250),
):
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()][:24]

    # Sequential rather than gathered: providers are rate limited, and hammering
    # them concurrently is the fastest way to a 429 on a paid plan.
    rows = []
    for sym in tickers:
        rows.append(await _symbol_row(
            sym, include_earnings=include_earnings,
            sparkline_points=sparkline_points))

    healthy = [r for r in rows if "error" not in r]
    ranks = [r["iv_rank"] for r in healthy if r.get("iv_rank") is not None]
    vrps = [r["variance_risk_premium"] for r in healthy
            if r.get("variance_risk_premium") is not None]

    # Names where options are priced above recent realized movement and IV sits
    # high in its own range -- the short-premium shortlist, not a recommendation.
    premium_candidates = sorted(
        [r for r in healthy
         if (r.get("iv_rank") or 0) >= 0.45
         and (r.get("variance_risk_premium") or -1) > 0],
        key=lambda r: (r.get("iv_rank") or 0), reverse=True)[:5]

    upcoming = sorted(
        [r for r in healthy if r.get("earnings")],
        key=lambda r: r["earnings"]["days_away"])[:6]

    return {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbols": tickers,
        "rows": rows,
        "summary": {
            "tracked": len(rows),
            "errors": len(rows) - len(healthy),
            "average_iv_rank": (sum(ranks) / len(ranks)) if ranks else None,
            "average_variance_premium": (sum(vrps) / len(vrps)) if vrps else None,
            "synthetic": any(r.get("synthetic") for r in healthy),
        },
        "premium_candidates": [
            {"symbol": r["symbol"], "iv_rank": r.get("iv_rank"),
             "variance_risk_premium": r.get("variance_risk_premium"),
             "atm_iv_30d": r.get("atm_iv_30d"),
             "earnings_days": (r.get("earnings") or {}).get("days_away")}
            for r in premium_candidates
        ],
        "earnings_soon": [
            {"symbol": r["symbol"], **r["earnings"]} for r in upcoming
        ],
    }


# --------------------------------------------------------------------------
# Market board and the impact-ranked news feed
# --------------------------------------------------------------------------

@router.get("/segments")
async def segments(days: int = Query(90, ge=30, le=400)):
    """
    The index / futures complex, sectors, and cross-asset, in one call.

    Rows that stand in for a futures contract say so on the row — see the note
    in services/market_map.py for why that substitution is made visible rather
    than smoothed over.
    """
    return await market_map.board(days=days)


@router.get("/news")
async def watchlist_news(
    symbols: str = Query("SPY,QQQ,NVDA,AAPL,TSLA,AMD"),
    limit: int = Query(40, ge=5, le=120),
):
    """
    Watchlist news, ranked by measured impact rather than by headline wording.

    Every item carries the components that produced its score and the window
    they were measured over, so the ordering can be checked. Items with no
    completed session after publication are unscored and sort last — they are
    not given a zero, because a zero would sort as "unimportant" when the truth
    is "not yet measurable".
    """
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()][:24]
    registry = get_registry()

    try:
        items = await registry.news(tickers, limit=limit)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    # Price context per symbol, fetched once and shared across that symbol's
    # items. The scorer needs bars whether or not the feed mentions the ticker,
    # so this is driven off the watchlist rather than off the returned items.
    contexts: dict[str, news_impact.SymbolContext] = {}
    for sym in tickers:
        try:
            bars = await registry.bars(sym, limit=120)
        except ProviderError:
            continue
        nxt = None
        try:
            events, _ = await earnings_service.load_events(sym)
            today = dt.date.today()
            upcoming = [e.event_date for e in events if e.event_date >= today]
            nxt = min(upcoming) if upcoming else None
        except Exception:                                      # noqa: BLE001
            pass
        contexts[sym] = news_impact.SymbolContext.build(sym, bars, next_earnings=nxt)

    ranked = news_impact.rank(items, contexts)
    ranked["symbols"] = tickers
    ranked["as_of"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return ranked
