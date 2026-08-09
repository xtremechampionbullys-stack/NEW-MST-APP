"""
Earnings event analytics.

Two ways to answer "how much does the market think this stock moves on
earnings", and the difference between them matters.

1. **ATM straddle / spot.** Not merely a rule of thumb -- it is exact. Under
   Black-Scholes the ATM straddle is approximately sqrt(2/pi) * S * sigma *
   sqrt(T) (Brenner-Subrahmanyam), and the expected absolute return of a
   zero-drift normal is sqrt(2/pi) * sigma * sqrt(T). Dividing one by the other
   leaves exactly straddle/S. So this *is* the risk-neutral expected absolute
   move over the life of that option.

   Its weakness is that "over the life of that option" includes every ordinary
   trading day as well as the event. On a 30-day expiry, most of that premium
   has nothing to do with earnings.

2. **Two-expiration decomposition** (Dubinsky & Johannes, 2006). Variance is
   additive, so for an option spanning a single announcement:

       (sigma_BS(T))^2 = sigma_diffusive^2 + (1/T) * sigma_jump^2

   where sigma_jump is a one-off event return standard deviation, *not*
   annualized. The 1/T weight is why the term structure slopes down into
   earnings: the same fixed jump variance is spread over less time.

   Take two expirations either side of the event and subtract to eliminate the
   diffusive term:

       sigma_jump^2 = [sigma_BS(T1)^2 - sigma_BS(T2)^2] / (1/T1 - 1/T2)

   Then the expected absolute event move is sqrt(2/pi) * sigma_jump.

Method 2 is the better number when the term structure cooperates. It does not
always: if the front expiry is not richer than the back, the subtraction yields
a negative variance, which means the chain is not pricing an event premium at
those two expiries. We return None in that case and say so, rather than
clamping to zero and presenting a fabricated number as a measurement.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from typing import Sequence

from .pricing import DAYS_PER_YEAR

# E|Z| for a standard normal: the mean absolute deviation constant.
MAD = math.sqrt(2.0 / math.pi)          # 0.7978845608...


@dataclass
class EventMove:
    method: str
    expected_move_pct: float | None
    expected_move_abs: float | None
    event_vol: float | None            # one-off jump sigma, in return units
    diffusive_vol: float | None        # annualized ex-event volatility
    front_dte: int | None = None
    back_dte: int | None = None
    front_iv: float | None = None
    back_iv: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def straddle_expected_move(call_mid: float, put_mid: float, spot: float,
                           dte: int | None = None) -> EventMove:
    """Expected absolute move implied by the ATM straddle."""
    if spot <= 0 or call_mid < 0 or put_mid < 0:
        return EventMove("straddle", None, None, None, None,
                         note="invalid straddle inputs")
    straddle = call_mid + put_mid
    pct = straddle / spot
    return EventMove(
        method="straddle",
        expected_move_pct=pct,
        expected_move_abs=straddle,
        event_vol=None,
        diffusive_vol=None,
        front_dte=dte,
        note=("Whole-life expected move from the ATM straddle. Includes ordinary "
              "day-to-day volatility as well as the event."),
    )


def decompose_event_vol(front_iv: float, front_dte: float,
                        back_iv: float, back_dte: float) -> tuple[float | None, float | None, str]:
    """
    Split implied variance into a one-off jump and a diffusive component.

    Returns (sigma_jump, sigma_diffusive, note). sigma_jump is a plain return
    standard deviation for the event itself -- it is not annualized, because the
    event is not a rate.
    """
    if front_dte <= 0 or back_dte <= 0:
        return None, None, "expirations must be in the future"
    if back_dte <= front_dte:
        return None, None, "the back expiration must be later than the front"
    if front_iv <= 0 or back_iv <= 0:
        return None, None, "both expirations need a positive implied volatility"

    t1 = front_dte / DAYS_PER_YEAR
    t2 = back_dte / DAYS_PER_YEAR

    numerator = front_iv * front_iv - back_iv * back_iv
    denominator = (1.0 / t1) - (1.0 / t2)
    if denominator <= 0:
        return None, None, "degenerate expiration pair"

    jump_var = numerator / denominator
    if jump_var <= 0:
        return None, None, (
            "The front expiration is not priced richer than the back, so the "
            "chain is not showing an event premium between these two dates. "
            "That is a real signal, not a failure: either the event is already "
            "past, it falls outside this window, or the market is not paying up "
            "for it.")

    sigma_jump = math.sqrt(jump_var)

    diffusive_var = front_iv * front_iv - jump_var / t1
    sigma_diffusive = math.sqrt(diffusive_var) if diffusive_var > 0 else None

    note = ("Event volatility isolated from ordinary volatility using the "
            "front/back implied variance difference.")
    if sigma_diffusive is None:
        note += (" The implied ex-event volatility came out negative, which "
                 "means the front expiry is almost entirely event premium -- "
                 "treat the diffusive figure as unavailable rather than zero.")
    return sigma_jump, sigma_diffusive, note


def event_expected_move(front_iv: float, front_dte: float, back_iv: float,
                        back_dte: float, spot: float) -> EventMove:
    """The isolated earnings move, in percent and dollars."""
    sigma_jump, sigma_diffusive, note = decompose_event_vol(
        front_iv, front_dte, back_iv, back_dte)

    if sigma_jump is None:
        return EventMove("term-structure", None, None, None, None,
                         front_dte=int(front_dte), back_dte=int(back_dte),
                         front_iv=front_iv, back_iv=back_iv, note=note)

    pct = MAD * sigma_jump
    return EventMove(
        method="term-structure",
        expected_move_pct=pct,
        expected_move_abs=pct * spot,
        event_vol=sigma_jump,
        diffusive_vol=sigma_diffusive,
        front_dte=int(front_dte),
        back_dte=int(back_dte),
        front_iv=front_iv,
        back_iv=back_iv,
        note=note,
    )


# --------------------------------------------------------------------------
# Expiry selection
# --------------------------------------------------------------------------

def select_event_expirations(expirations: Sequence[dt.date], event_date: dt.date,
                             next_event: dt.date | None = None
                             ) -> tuple[dt.date | None, dt.date | None, str]:
    """
    Pick the expiration pair that brackets exactly one announcement.

    The front leg must be the first expiration *after* the event, so it contains
    the jump. The back leg must be later still and -- critically -- must not
    contain a second announcement, or the subtraction is measuring the
    difference between one event and two, which is meaningless.
    """
    future = sorted(e for e in expirations if e > event_date)
    if not future:
        return None, None, "no expiration falls after the announcement"

    front = future[0]
    candidates = [e for e in future[1:] if next_event is None or e < next_event]
    if not candidates:
        return front, None, (
            "no clean back expiration: every later expiry either does not exist "
            "or spans the following quarter's announcement")

    # Prefer a back expiry meaningfully further out. Adjacent weeklies differ by
    # days, and the 1/T weighting amplifies quote noise in a short front leg.
    target = front + dt.timedelta(days=21)
    back = min(candidates, key=lambda e: abs((e - target).days))
    return front, back, "ok"


def spans_event(entry_dte: float, event_date: dt.date,
                today: dt.date | None = None) -> bool:
    """Does a position held to `entry_dte` days still be open on the event date?"""
    today = today or dt.date.today()
    expiry = today + dt.timedelta(days=int(round(entry_dte)))
    return today <= event_date <= expiry


def days_until(event_date: dt.date, today: dt.date | None = None) -> int:
    return (event_date - (today or dt.date.today())).days


# --------------------------------------------------------------------------
# Historical reaction
# --------------------------------------------------------------------------

def historical_reactions(bars: Sequence, event_dates: Sequence[dt.date],
                         session_map: dict[dt.date, str] | None = None) -> dict:
    """
    Realized move on each past announcement, for comparison against what the
    chain is charging today.

    Session handling matters: a company reporting after the close moves on the
    *next* session, one reporting before the open moves on the same session.
    Getting this wrong systematically understates the reaction.
    """
    if not bars or not event_dates:
        return {"count": 0, "moves": [], "average_abs_move": None,
                "max_abs_move": None, "median_abs_move": None}

    by_date: dict[dt.date, Any] = {}
    ordered: list[dt.date] = []
    for b in bars:
        day = b.t.date() if hasattr(b.t, "date") else b.t
        by_date[day] = b
        ordered.append(day)
    ordered.sort()

    moves: list[dict] = []
    for ed in sorted(event_dates):
        session = (session_map or {}).get(ed, "amc")
        after = [d for d in ordered if d > ed] if session in ("amc", "unknown") else \
                [d for d in ordered if d >= ed]
        before = [d for d in ordered if d <= ed] if session in ("amc", "unknown") else \
                 [d for d in ordered if d < ed]
        if not after or not before:
            continue
        prior = by_date[before[-1]]
        reaction = by_date[after[0]]
        if not prior.c:
            continue
        change = (reaction.c / prior.c) - 1.0
        moves.append({
            "event_date": ed.isoformat(),
            "session": session,
            "reaction_date": after[0].isoformat(),
            "prior_close": prior.c,
            "reaction_close": reaction.c,
            "move_pct": round(change, 5),
            "abs_move_pct": round(abs(change), 5),
        })

    if not moves:
        return {"count": 0, "moves": [], "average_abs_move": None,
                "max_abs_move": None, "median_abs_move": None}

    abs_moves = sorted(m["abs_move_pct"] for m in moves)
    mid = len(abs_moves) // 2
    median = (abs_moves[mid] if len(abs_moves) % 2
              else (abs_moves[mid - 1] + abs_moves[mid]) / 2)

    return {
        "count": len(moves),
        "moves": sorted(moves, key=lambda m: m["event_date"], reverse=True),
        "average_abs_move": round(sum(abs_moves) / len(abs_moves), 5),
        "median_abs_move": round(median, 5),
        "max_abs_move": abs_moves[-1],
    }
