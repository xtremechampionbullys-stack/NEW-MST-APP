"""
Backtest orchestration.

Pulls the bars, builds the right quote source, loads the earnings calendar if
the rules ask for it, runs the engine, and persists the result.

The quote-source decision is made here and it is the only place it is made:
if the `option_quotes` archive covers the requested window we use it, otherwise
we fall back to the modelled surface. Either way the choice is reported, never
inferred by the caller.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text

from ..db.engine import get_database
from ..db.store import get_store
from ..engine.backtest import (
    BacktestRules,
    ModelledQuoteSource,
    StoredQuoteSource,
    pl_distribution,
    run_backtest,
)
from ..providers.base import ProviderError
from ..providers.registry import get_registry
from . import earnings_service

log = logging.getLogger(__name__)


async def _load_stored_quotes(symbol: str, start: dt.date,
                              end: dt.date) -> dict | None:
    """
    Read historical implied vols out of `option_quotes`, if any exist.

    Returns None when the archive is empty for this symbol and window, which is
    the normal case until an ingest job has been run. Deliberately quiet about
    it: falling back is expected, not exceptional.
    """
    db = get_database()
    if not db.available or db.session_factory is None:
        return None

    try:
        async with db.session_factory() as session:
            result = await session.execute(
                text("""
                    SELECT ts, expiration, strike, right, implied_vol
                    FROM option_quotes
                    WHERE underlying = :sym
                      AND ts >= :start AND ts <= :end
                      AND implied_vol IS NOT NULL
                """),
                {"sym": symbol.upper(), "start": start, "end": end},
            )
            rows = result.fetchall()
    except Exception as exc:                                   # noqa: BLE001
        # The table may not exist yet on a fresh SQLite file -- that is a
        # fallback condition, not an error worth surfacing.
        log.debug("option_quotes unavailable for %s: %s", symbol, exc)
        return None

    if not rows:
        return None

    quotes: dict[tuple[dt.date, float, float, str], float] = {}
    for ts, expiration, strike, right, iv in rows:
        day = ts.date() if hasattr(ts, "date") else ts
        exp = expiration if isinstance(expiration, dt.date) else \
            dt.date.fromisoformat(str(expiration)[:10])
        dte = (exp - day).days
        quotes[(day, float(round(dte, 0)), round(float(strike), 2), right)] = float(iv)
    return quotes


async def run(symbol: str, rules: BacktestRules, *, lookback_days: int = 730,
              persist: bool = True, label: str = "") -> dict:
    registry = get_registry()

    # Ask for extra bars: the engine needs a warm-up window before the first
    # entry so trailing realized volatility is meaningful on day one.
    limit = min(max(lookback_days, 200), 5000)
    try:
        bars = await registry.bars(symbol, limit=limit)
    except ProviderError as exc:
        raise ProviderError(f"no price history for {symbol}: {exc}") from exc

    if len(bars) < 120:
        raise ProviderError(
            f"only {len(bars)} bars available for {symbol}; a backtest needs at "
            f"least 120")

    dates = [b.t.date() for b in bars]
    closes_by_date = {d: b.c for d, b in zip(dates, bars)}

    modelled = ModelledQuoteSource(
        closes_by_date=closes_by_date, ordered_dates=dates,
        variance_risk_premium=rules.variance_risk_premium)

    stored = await _load_stored_quotes(symbol, dates[0], dates[-1])
    source = (StoredQuoteSource(quotes=stored, fallback=modelled)
              if stored else modelled)

    earnings_dates: list[dt.date] = []
    if rules.skip_earnings:
        try:
            events, _ = await earnings_service.load_events(
                symbol, lookback_quarters=16, lookahead_days=30)
            earnings_dates = [e.event_date for e in events]
        except Exception as exc:                               # noqa: BLE001
            log.info("earnings calendar unavailable for %s: %s", symbol, exc)

    result = run_backtest(bars, rules, quote_source=source, symbol=symbol.upper(),
                          earnings_dates=earnings_dates)
    result["distribution"] = pl_distribution(result["trades"])

    if rules.skip_earnings and not earnings_dates:
        result.setdefault("warnings", []).append(
            "Earnings avoidance was requested but no calendar was available, so "
            "no entries were skipped.")

    if persist:
        try:
            saved = await get_store().save_backtest(result, label=label)
            result["id"] = saved["id"]
            result["persisted"] = True
            result["backend"] = get_store().backend
        except Exception as exc:                               # noqa: BLE001
            log.warning("could not persist backtest: %s", exc)
            result["persisted"] = False

    return result


async def compare(symbol: str, rule_sets: list[BacktestRules], *,
                  lookback_days: int = 730) -> dict:
    """
    Run several rule sets over identical history.

    This is the comparison worth making -- same bars, same quote source, same
    dates -- so any difference is attributable to the rules rather than to which
    window happened to be sampled.
    """
    runs = []
    for rules in rule_sets[:6]:
        try:
            result = await run(symbol, rules, lookback_days=lookback_days,
                               persist=False)
        except (ProviderError, ValueError) as exc:
            runs.append({"template": rules.template_id, "error": str(exc)})
            continue
        runs.append({
            "template": result["template"],
            "template_name": result["template_name"],
            "rules": result["rules"],
            "metrics": result["metrics"],
            "quote_source": result["quote_source"],
        })
    return {"symbol": symbol.upper(), "runs": runs}
