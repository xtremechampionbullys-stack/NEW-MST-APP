"""
Earnings service.

Joins three things the rest of the app wants as one answer: the calendar date,
what the option chain is charging for the event, and what the stock has actually
done on past announcements.

That third piece is the one people skip, and it is the one that decides the
trade. A 9% implied move is expensive or cheap only relative to the 4% or 12%
this name typically delivers.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..db.store import get_store
from ..engine.earnings import (
    days_until,
    event_expected_move,
    historical_reactions,
    select_event_expirations,
    straddle_expected_move,
)
from ..providers.base import EarningsEvent, OptionChain, ProviderError
from ..providers.registry import get_registry

log = logging.getLogger(__name__)


async def load_events(symbol: str, *, lookback_quarters: int = 8,
                      lookahead_days: int = 120,
                      refresh: bool = False) -> tuple[list[EarningsEvent], str]:
    """
    Cached-first read. Earnings dates change at most a few times a quarter while
    the good sources are metered, so the database is the primary read path and
    the provider is consulted on a miss or an explicit refresh.
    """
    store = get_store()
    since = dt.date.today() - dt.timedelta(days=int(lookback_quarters * 91.3))
    until = dt.date.today() + dt.timedelta(days=lookahead_days)

    if not refresh:
        cached = await store.get_earnings(symbol, since=since, until=until)
        if cached:
            events = [_from_row(r) for r in cached]
            has_future = any(e.event_date >= dt.date.today() for e in events)
            if has_future:
                return events, "cache"

    registry = get_registry()
    events = await registry.earnings(symbol, lookback_quarters=lookback_quarters,
                                     lookahead_days=lookahead_days)
    try:
        await store.upsert_earnings([{
            "symbol": e.symbol, "event_date": e.event_date, "session": e.session,
            "date_status": e.date_status, "fiscal_period": e.fiscal_period,
            "fiscal_year": e.fiscal_year, "eps_estimate": e.eps_estimate,
            "eps_actual": e.eps_actual, "revenue_estimate": e.revenue_estimate,
            "revenue_actual": e.revenue_actual, "source": e.source,
            "is_synthetic": e.is_synthetic,
        } for e in events])
    except Exception as exc:                                   # noqa: BLE001
        log.warning("could not cache earnings for %s: %s", symbol, exc)

    return events, events[0].source if events else "none"


def _from_row(r: dict) -> EarningsEvent:
    return EarningsEvent(
        symbol=r["symbol"],
        event_date=dt.date.fromisoformat(r["event_date"][:10]),
        session=r.get("session", "unknown"),
        date_status=r.get("date_status", "projected"),
        fiscal_period=r.get("fiscal_period"),
        fiscal_year=r.get("fiscal_year"),
        eps_estimate=r.get("eps_estimate"),
        eps_actual=r.get("eps_actual"),
        revenue_estimate=r.get("revenue_estimate"),
        revenue_actual=r.get("revenue_actual"),
        source=r.get("source", ""),
        is_synthetic=bool(r.get("is_synthetic")),
    )


def next_event(events: list[EarningsEvent],
               today: dt.date | None = None) -> EarningsEvent | None:
    today = today or dt.date.today()
    upcoming = [e for e in events if e.event_date >= today]
    return min(upcoming, key=lambda e: e.event_date) if upcoming else None


def following_event(events: list[EarningsEvent],
                    after: dt.date) -> EarningsEvent | None:
    later = [e for e in events if e.event_date > after]
    return min(later, key=lambda e: e.event_date) if later else None


def _atm_pair(chain: OptionChain, expiration: dt.date):
    contracts = chain.for_expiration(expiration)
    calls = [c for c in contracts if c.right == "call"]
    puts = [c for c in contracts if c.right == "put"]
    if not calls or not puts:
        return None, None
    call = min(calls, key=lambda c: abs(c.strike - chain.spot))
    put = min(puts, key=lambda c: abs(c.strike - chain.spot))
    return call, put


def _atm_iv(chain: OptionChain, expiration: dt.date) -> float | None:
    """Average the implied vol of the ATM call and put -- more stable than either."""
    call, put = _atm_pair(chain, expiration)
    ivs = [c.implied_vol for c in (call, put) if c and c.implied_vol]
    return sum(ivs) / len(ivs) if ivs else None


def implied_move(chain: OptionChain, event: EarningsEvent,
                 next_after: EarningsEvent | None = None) -> dict:
    """
    Both estimates of the earnings move, side by side.

    The straddle number covers the whole life of the front option -- event plus
    every ordinary day. The term-structure number isolates the event itself.
    They should differ, and the gap is informative: a small gap on a 7-day
    expiry means the event dominates; a large gap on a 45-day expiry means most
    of what you would pay for is not the event at all.
    """
    expirations = chain.expirations()
    front, back, note = select_event_expirations(
        expirations, event.event_date,
        next_after.event_date if next_after else None)

    result: dict = {
        "event_date": event.event_date.isoformat(),
        "days_away": days_until(event.event_date),
        "session": event.session,
        "date_status": event.date_status,
        "front_expiration": front.isoformat() if front else None,
        "back_expiration": back.isoformat() if back else None,
        "selection_note": note,
        "straddle": None,
        "term_structure": None,
        "preferred": None,
    }

    if front:
        call, put = _atm_pair(chain, front)
        if call and put and call.mid and put.mid:
            sm = straddle_expected_move(call.mid, put.mid, chain.spot,
                                        (front - dt.date.today()).days)
            result["straddle"] = sm.to_dict()
            result["straddle"]["atm_strike"] = call.strike

    if front and back:
        iv_front = _atm_iv(chain, front)
        iv_back = _atm_iv(chain, back)
        if iv_front and iv_back:
            dte_front = (front - dt.date.today()).days
            dte_back = (back - dt.date.today()).days
            em = event_expected_move(iv_front, dte_front, iv_back, dte_back,
                                     chain.spot)
            result["term_structure"] = em.to_dict()

    ts = result.get("term_structure")
    if ts and ts.get("expected_move_pct"):
        result["preferred"] = "term-structure"
    elif result.get("straddle"):
        result["preferred"] = "straddle"

    return result


async def earnings_report(symbol: str, *, refresh: bool = False,
                          lookback_quarters: int = 8) -> dict:
    """The whole picture for one symbol."""
    registry = get_registry()
    events, source = await load_events(symbol, refresh=refresh,
                                       lookback_quarters=lookback_quarters)

    upcoming = next_event(events)
    following = following_event(events, upcoming.event_date) if upcoming else None

    move: dict | None = None
    chain_source = None
    if upcoming:
        try:
            chain = await registry.chain(symbol, max_expirations=10)
            chain_source = chain.source
            move = implied_move(chain, upcoming, following)
        except ProviderError as exc:
            log.info("no chain for %s implied move: %s", symbol, exc)

    history: dict = {"count": 0, "moves": []}
    past = [e for e in events if e.event_date < dt.date.today()]
    if past:
        try:
            bars = await registry.bars(symbol, limit=760)
            history = historical_reactions(
                bars, [e.event_date for e in past],
                {e.event_date: e.session for e in past})
        except ProviderError as exc:
            log.info("no bars for %s earnings history: %s", symbol, exc)

    # The comparison that decides the trade.
    verdict = None
    if move and history.get("average_abs_move"):
        block = (move.get("term_structure") if move.get("preferred") == "term-structure"
                 else move.get("straddle")) or {}
        implied = block.get("expected_move_pct")
        realized = history["average_abs_move"]
        if implied and realized:
            ratio = implied / realized
            verdict = {
                "implied_move_pct": implied,
                "average_realized_pct": realized,
                "ratio": round(ratio, 3),
                "reading": (
                    "The chain is pricing a larger move than this name has "
                    "typically delivered. Selling the event has historically had "
                    "an edge here -- with the caveat that the one time it does "
                    "not is the time that matters."
                    if ratio > 1.15 else
                    "The chain is pricing a smaller move than this name has "
                    "typically delivered. Buying the event looks comparatively "
                    "cheap, though a small sample can flatter this badly."
                    if ratio < 0.85 else
                    "The implied move is close to this name's historical average "
                    "reaction. No obvious edge either way."),
            }

    return {
        "symbol": symbol.upper(),
        "source": source,
        "chain_source": chain_source,
        "synthetic": any(e.is_synthetic for e in events),
        "next_event": upcoming.to_dict() if upcoming else None,
        "following_event": following.to_dict() if following else None,
        "implied_move": move,
        "history": history,
        "verdict": verdict,
        "events": [e.to_dict() for e in events],
    }


async def events_in_window(symbol: str, dte: float) -> list[EarningsEvent]:
    """Earnings announcements that fall between today and a given expiry."""
    try:
        events, _ = await load_events(symbol)
    except ProviderError:
        return []
    today = dt.date.today()
    expiry = today + dt.timedelta(days=int(round(dte)))
    return [e for e in events if today <= e.event_date <= expiry]
