"""
Company research: profiles, fundamentals, derived metrics, and the news feed.

The fundamentals endpoint does the ratio work server-side rather than shipping
raw statements to the browser, so the same numbers feed the UI, the screener,
and any agent that asks -- there is exactly one definition of gross margin in
the system.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..providers.base import ProviderError
from ..providers.registry import get_registry

router = APIRouter(prefix="/api/research", tags=["research"])


def _provider_http_error(exc: ProviderError) -> HTTPException:
    return HTTPException(503 if exc.provider == "configuration" else 502, str(exc))


@router.get("/profile/{symbol}")
async def profile(symbol: str, provider: str | None = None):
    try:
        p = await get_registry().profile(symbol, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc
    return p.to_dict()


@router.get("/fundamentals/{symbol}")
async def fundamentals(symbol: str, timeframe: str = "quarterly",
                       limit: int = Query(8, ge=1, le=40),
                       provider: str | None = None):
    registry = get_registry()
    try:
        rows = await registry.fundamentals(symbol, timeframe=timeframe,
                                           limit=limit, provider=provider)
        profile_data = await registry.profile(symbol, provider=provider)
        quote = await registry.quote(symbol, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    periods = [r.to_dict() for r in rows]
    latest = rows[0] if rows else None

    ttm_revenue = _sum_field(rows[:4], "revenue") if timeframe == "quarterly" \
        else (latest.revenue if latest else None)
    ttm_net = _sum_field(rows[:4], "net_income") if timeframe == "quarterly" \
        else (latest.net_income if latest else None)
    ttm_eps = _sum_field(rows[:4], "eps_diluted") or _sum_field(rows[:4], "eps_basic")

    market_cap = profile_data.market_cap
    if not market_cap and profile_data.shares_outstanding:
        market_cap = profile_data.shares_outstanding * quote.last

    derived = {
        "market_cap": market_cap,
        "price": quote.last,
        "pe_ttm": _ratio(quote.last, ttm_eps),
        "ps_ttm": _ratio(market_cap, ttm_revenue),
        "profit_margin_ttm": _ratio(ttm_net, ttm_revenue),
        "revenue_growth_yoy": _growth(rows, "revenue",
                                      4 if timeframe == "quarterly" else 1),
        "net_income_growth_yoy": _growth(rows, "net_income",
                                         4 if timeframe == "quarterly" else 1),
        "roe": latest.roe if latest else None,
        "debt_to_equity": latest.debt_to_equity if latest else None,
        "current_ratio": latest.current_ratio if latest else None,
        "free_cash_flow_ttm": _sum_field(rows[:4], "free_cash_flow")
        if timeframe == "quarterly" else (latest.free_cash_flow if latest else None),
    }

    return {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "profile": profile_data.to_dict(),
        "derived": derived,
        "periods": periods,
        "synthetic": any(p.get("ticker") and not market_cap for p in periods[:1])
        or quote.source == "synthetic",
    }


@router.get("/news")
async def news(symbols: str = Query(..., description="Comma separated tickers"),
               limit: int = Query(25, ge=1, le=100),
               media: str = Query("all", pattern="^(all|article|video)$"),
               provider: str | None = None):
    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        raise HTTPException(422, "at least one symbol is required")
    try:
        items = await get_registry().news(tickers, limit=limit, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    if media != "all":
        items = [i for i in items if i.media_type == media]

    return {
        "symbols": tickers,
        "count": len(items),
        "items": [i.to_dict() for i in items],
    }


def _sum_field(rows, field: str):
    values = [getattr(r, field) for r in rows if getattr(r, field) is not None]
    return sum(values) if values else None


def _ratio(num, den):
    try:
        if num is None or not den:
            return None
        return round(float(num) / float(den), 4)
    except (TypeError, ZeroDivisionError, ValueError):
        return None


def _growth(rows, field: str, lag: int):
    if len(rows) <= lag:
        return None
    now = getattr(rows[0], field)
    then = getattr(rows[lag], field)
    if now is None or not then:
        return None
    try:
        return round(now / then - 1, 4)
    except ZeroDivisionError:
        return None
