"""
Volatility history and IV rank.

IV rank is defined against a trailing window of the name's *own* implied
volatility -- conventionally 52 weeks:

    IV rank = (IV_today - IV_min) / (IV_max - IV_min)

Without stored history there is nothing to rank against. The usual workaround is
to take the spread of implied vols currently quoted across the chain and rank
today's ATM against that. It is better than nothing, but it measures a different
thing entirely -- the cross-section of strikes and expiries right now, not this
name's own history -- and it is systematically too narrow, which makes IV rank
drift toward the middle and lose most of its signal.

So both are implemented, the stored-history version is preferred whenever there
is enough of it, and every response says which one produced the number. A rank
computed from 6 days of history dressed up as a 52-week rank would be worse than
admitting the history is not there yet.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import get_settings
from ..db.store import get_store
from ..engine.vol import iv_rank as cross_sectional_rank
from ..engine.vol import realized_vol
from ..providers.base import ProviderError
from ..providers.registry import get_registry

log = logging.getLogger(__name__)


def atm_iv_from_chain(chain, target_dte: int = 30) -> float | None:
    """
    ATM implied vol at a target maturity, interpolated in total variance across
    the two bracketing expiries. Picking the nearest expiry instead would make
    the series jump every time one expiration rolls off, which shows up as fake
    volatility-of-volatility in the stored history.
    """
    spot = chain.spot
    by_exp: dict[int, list[float]] = {}
    for c in chain.contracts:
        if not c.implied_vol or c.dte <= 0:
            continue
        if abs(c.strike / spot - 1.0) > 0.05:
            continue
        by_exp.setdefault(c.dte, []).append(c.implied_vol)

    if not by_exp:
        return None

    points = sorted((dte, sum(v) / len(v)) for dte, v in by_exp.items())
    if len(points) == 1:
        return points[0][1]

    below = [p for p in points if p[0] <= target_dte]
    above = [p for p in points if p[0] >= target_dte]
    if not below:
        return above[0][1]
    if not above:
        return below[-1][1]

    d0, v0 = below[-1]
    d1, v1 = above[0]
    if d0 == d1:
        return v0

    w0, w1 = v0 * v0 * d0, v1 * v1 * d1
    w = w0 + (w1 - w0) * (target_dte - d0) / (d1 - d0)
    return (max(w, 1e-9) / max(target_dte, 1e-9)) ** 0.5


async def snapshot_symbol(symbol: str) -> dict:
    """
    Record one day's ATM IV. Intended to run once daily after the close; the
    store overwrites rather than appends within a day, so running it more often
    is harmless and a restart cannot double-weight a day.
    """
    registry = get_registry()
    chain = await registry.chain(symbol, max_expirations=8)
    atm = atm_iv_from_chain(chain, 30)
    if atm is None:
        raise ProviderError(f"no implied volatility available for {symbol}")

    rv = None
    try:
        bars = await registry.bars(symbol, limit=60)
        rv = realized_vol([b.c for b in bars], 20)
    except ProviderError:
        pass

    row = await get_store().record_iv(
        symbol, atm, realized_20d=rv, spot=chain.spot, source=chain.source)
    return row


async def snapshot_many(symbols: list[str]) -> dict:
    ok, failed = [], {}
    for sym in symbols:
        try:
            await snapshot_symbol(sym)
            ok.append(sym.upper())
        except Exception as exc:                              # noqa: BLE001
            failed[sym.upper()] = str(exc)
    return {"recorded": ok, "failed": failed,
            "backend": get_store().backend}


async def iv_rank_for(symbol: str, current_iv: float,
                      fallback_pool: list[float] | None = None) -> dict:
    """
    IV rank, preferring stored history and saying which method it used.
    """
    settings = get_settings()
    store = get_store()

    try:
        history = await store.iv_history(symbol, days=settings.iv_rank_window_days)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("iv history read failed for %s: %s", symbol, exc)
        history = []

    values = [h["atm_iv_30d"] for h in history if h.get("atm_iv_30d")]

    if len(values) >= settings.iv_rank_min_observations:
        lo, hi = min(values), max(values)
        rank = (current_iv - lo) / (hi - lo) if hi > lo else 0.5
        pctile = sum(1 for v in values if v < current_iv) / len(values)
        return {
            "iv_rank": max(0.0, min(1.0, rank)),
            "iv_percentile": pctile,
            "low": lo,
            "high": hi,
            "observations": len(values),
            "window_days": settings.iv_rank_window_days,
            "method": "history",
            "note": (f"Ranked against {len(values)} stored daily observations "
                     f"over the past {settings.iv_rank_window_days} days."),
        }

    result = cross_sectional_rank(current_iv, fallback_pool or [])
    result.update({
        "observations": len(values),
        "window_days": settings.iv_rank_window_days,
        "method": "cross-sectional",
        "note": (
            f"Only {len(values)} stored observation(s) — below the "
            f"{settings.iv_rank_min_observations} needed for a historical rank. "
            f"This figure ranks today's ATM against the spread of implied "
            f"volatilities currently quoted across the chain, which is a "
            f"different and narrower measure. Run the daily snapshot to build "
            f"real history."),
    })
    return result


async def iv_history_series(symbol: str, days: int = 365) -> dict:
    rows = await get_store().iv_history(symbol, days=days)
    return {
        "symbol": symbol.upper(),
        "days": days,
        "observations": len(rows),
        "backend": get_store().backend,
        "series": rows,
    }


async def backfill_from_chain(symbol: str, days: int = 1) -> dict:
    """
    Seed a single observation for today.

    Deliberately not a real backfill: historical implied volatility cannot be
    reconstructed from a current chain, and any provider that could supply it
    (Massive option aggregates by contract) needs a per-contract walk that
    belongs in a batch job, not a request handler. Pretending to backfill by
    repeating today's value would produce a flat series and an IV rank of
    exactly 0.5 forever.
    """
    row = await snapshot_symbol(symbol)
    return {
        "recorded": row,
        "note": ("Recorded today only. True historical implied volatility has to "
                 "be rebuilt from per-contract history in a batch job; it cannot "
                 "be inferred from today's chain."),
    }
