"""
Earnings analytics tests.

The implied-move math is checked against closed-form identities rather than
against remembered numbers: if the straddle formula really is the risk-neutral
expected absolute move, then pricing a straddle at a known volatility and
running it back through the formula must recover that volatility's own expected
move. Same for the two-expiration decomposition -- construct implied vols from a
known jump, and the solver has to return the jump.
"""

import datetime as dt
import math

import pytest

from app.engine.earnings import (
    MAD,
    decompose_event_vol,
    days_until,
    event_expected_move,
    historical_reactions,
    select_event_expirations,
    spans_event,
    straddle_expected_move,
)
from app.engine.pricing import DAYS_PER_YEAR, bs_price
from app.providers.base import Bar


# --------------------------------------------------------------------------
# Straddle
# --------------------------------------------------------------------------

def test_straddle_recovers_the_expected_absolute_move():
    """
    The ATM straddle divided by spot IS E|return| under Black-Scholes, not an
    approximation to it. Price a straddle at a known sigma and the formula must
    return sqrt(2/pi) * sigma * sqrt(T).
    """
    S, T, sigma = 100.0, 30 / DAYS_PER_YEAR, 0.45
    r = q = 0.0                       # zero drift, so the identity is exact
    call = bs_price(S, S, T, r, q, sigma, "call")
    put = bs_price(S, S, T, r, q, sigma, "put")

    move = straddle_expected_move(call, put, S, 30)
    expected = MAD * sigma * math.sqrt(T)
    assert move.expected_move_pct == pytest.approx(expected, rel=0.01)


def test_straddle_scales_with_sqrt_time():
    S, sigma, r, q = 200.0, 0.35, 0.0, 0.0
    moves = []
    for dte in (30, 120):
        T = dte / DAYS_PER_YEAR
        c = bs_price(S, S, T, r, q, sigma, "call")
        p = bs_price(S, S, T, r, q, sigma, "put")
        moves.append(straddle_expected_move(c, p, S, dte).expected_move_pct)
    assert moves[1] / moves[0] == pytest.approx(2.0, rel=0.03)


def test_straddle_rejects_nonsense_inputs():
    assert straddle_expected_move(1.0, 1.0, 0.0).expected_move_pct is None


# --------------------------------------------------------------------------
# Two-expiration decomposition
# --------------------------------------------------------------------------

def _implied_from_components(diffusive: float, jump: float, dte: float) -> float:
    """Forward direction of the model: build the observable IV from components."""
    T = dte / DAYS_PER_YEAR
    return math.sqrt(diffusive ** 2 + (jump ** 2) / T)


@pytest.mark.parametrize("jump", [0.04, 0.08, 0.15])
@pytest.mark.parametrize("diffusive", [0.20, 0.45])
@pytest.mark.parametrize("pair", [(7, 35), (14, 45), (21, 60)])
def test_decomposition_recovers_a_known_jump(jump, diffusive, pair):
    """
    Round-trip: synthesize two implied vols from a known diffusive vol and a
    known event jump, then check the solver recovers both.
    """
    dte1, dte2 = pair
    iv1 = _implied_from_components(diffusive, jump, dte1)
    iv2 = _implied_from_components(diffusive, jump, dte2)

    solved_jump, solved_diffusive, _ = decompose_event_vol(iv1, dte1, iv2, dte2)
    assert solved_jump == pytest.approx(jump, rel=1e-6)
    assert solved_diffusive == pytest.approx(diffusive, rel=1e-6)


def test_decomposition_expected_move_applies_the_mad_constant():
    diffusive, jump, dte1, dte2 = 0.30, 0.09, 10, 45
    iv1 = _implied_from_components(diffusive, jump, dte1)
    iv2 = _implied_from_components(diffusive, jump, dte2)

    move = event_expected_move(iv1, dte1, iv2, dte2, spot=150.0)
    assert move.method == "term-structure"
    assert move.event_vol == pytest.approx(jump, rel=1e-6)
    assert move.expected_move_pct == pytest.approx(MAD * jump, rel=1e-9)
    assert move.expected_move_abs == pytest.approx(MAD * jump * 150.0, rel=1e-9)


def test_decomposition_reports_no_event_premium_rather_than_faking_one():
    """
    An upward-sloping term structure means the chain is not pricing an event
    between those dates. That must come back as None with an explanation, not as
    a clamped zero dressed up as a measurement.
    """
    jump, diffusive = 0.0, 0.30
    iv1 = _implied_from_components(diffusive, jump, 10)
    iv2 = _implied_from_components(diffusive, jump, 45) * 1.10   # back richer

    solved, _, note = decompose_event_vol(iv1, 10, iv2, 45)
    assert solved is None
    assert "event premium" in note.lower()

    move = event_expected_move(iv1, 10, iv2, 45, spot=100.0)
    assert move.expected_move_pct is None


def test_decomposition_guards_degenerate_pairs():
    assert decompose_event_vol(0.5, 30, 0.4, 30)[0] is None    # same maturity
    assert decompose_event_vol(0.5, 45, 0.4, 20)[0] is None    # back before front
    assert decompose_event_vol(0.0, 10, 0.4, 45)[0] is None    # no front vol
    assert decompose_event_vol(0.5, 0, 0.4, 45)[0] is None     # expired front


def test_front_expiry_is_more_event_sensitive_than_back():
    """The 1/T weighting is the whole reason the term structure inverts."""
    diffusive, jump = 0.28, 0.10
    iv_short = _implied_from_components(diffusive, jump, 5)
    iv_long = _implied_from_components(diffusive, jump, 90)
    assert iv_short > iv_long
    assert iv_short > diffusive * 2


# --------------------------------------------------------------------------
# Expiry selection
# --------------------------------------------------------------------------

def test_selects_first_expiry_after_the_event():
    event = dt.date(2026, 8, 20)
    exps = [dt.date(2026, 8, 14), dt.date(2026, 8, 21), dt.date(2026, 8, 28),
            dt.date(2026, 9, 18), dt.date(2026, 10, 16)]
    front, back, note = select_event_expirations(exps, event)
    assert front == dt.date(2026, 8, 21)
    assert back is not None and back > front
    assert note == "ok"


def test_back_expiry_never_spans_the_next_announcement():
    """
    Subtracting an expiry that contains two events from one that contains one
    is not measuring a single jump. The selector must exclude it.
    """
    event = dt.date(2026, 8, 20)
    next_event = dt.date(2026, 11, 19)
    exps = [dt.date(2026, 8, 21), dt.date(2026, 9, 18), dt.date(2026, 12, 18)]
    front, back, _ = select_event_expirations(exps, event, next_event)
    assert front == dt.date(2026, 8, 21)
    assert back == dt.date(2026, 9, 18)          # not the December expiry


def test_reports_when_no_clean_back_expiry_exists():
    event = dt.date(2026, 8, 20)
    next_event = dt.date(2026, 11, 19)
    exps = [dt.date(2026, 8, 21), dt.date(2026, 12, 18)]
    front, back, note = select_event_expirations(exps, event, next_event)
    assert front == dt.date(2026, 8, 21)
    assert back is None
    assert "clean back expiration" in note


def test_no_expiry_after_the_event():
    front, back, note = select_event_expirations(
        [dt.date(2026, 8, 1)], dt.date(2026, 8, 20))
    assert front is None and back is None
    assert "after the announcement" in note


# --------------------------------------------------------------------------
# Window checks
# --------------------------------------------------------------------------

def test_spans_event_window():
    today = dt.date(2026, 7, 30)
    assert spans_event(45, dt.date(2026, 8, 20), today)
    assert not spans_event(10, dt.date(2026, 8, 20), today)
    assert not spans_event(45, dt.date(2026, 7, 1), today)     # already past
    assert spans_event(45, today, today)                        # today counts


def test_days_until():
    today = dt.date(2026, 7, 30)
    assert days_until(dt.date(2026, 8, 9), today) == 10
    assert days_until(dt.date(2026, 7, 20), today) == -10


# --------------------------------------------------------------------------
# Historical reactions
# --------------------------------------------------------------------------

def _bars(prices: dict[dt.date, float]) -> list[Bar]:
    return [Bar(t=dt.datetime.combine(d, dt.time(20, 0), dt.timezone.utc),
                o=p, h=p, l=p, c=p, v=1.0)
            for d, p in sorted(prices.items())]


def test_after_close_report_measures_the_next_session():
    """
    A company reporting after the close moves the *following* session. Measuring
    the same session would systematically report near-zero reactions.
    """
    event = dt.date(2026, 6, 10)
    bars = _bars({
        dt.date(2026, 6, 9): 100.0,
        event: 101.0,            # report lands after this close
        dt.date(2026, 6, 11): 110.0,
    })
    result = historical_reactions(bars, [event], {event: "amc"})
    assert result["count"] == 1
    move = result["moves"][0]
    assert move["prior_close"] == 101.0
    assert move["reaction_close"] == 110.0
    # Reactions are stored rounded to 5 decimal places -- 0.001% on a return.
    assert move["move_pct"] == pytest.approx(110.0 / 101.0 - 1, abs=1e-5)


def test_before_open_report_measures_the_same_session():
    event = dt.date(2026, 6, 10)
    bars = _bars({
        dt.date(2026, 6, 9): 100.0,
        event: 92.0,             # report landed before this open
        dt.date(2026, 6, 11): 93.0,
    })
    result = historical_reactions(bars, [event], {event: "bmo"})
    move = result["moves"][0]
    assert move["prior_close"] == 100.0
    assert move["reaction_close"] == 92.0
    assert move["move_pct"] == pytest.approx(-0.08, abs=1e-5)


def test_reaction_summary_statistics():
    bars = _bars({dt.date(2026, 1, i): 100.0 + (i % 3) * 5 for i in range(1, 28)})
    events = [dt.date(2026, 1, 5), dt.date(2026, 1, 12), dt.date(2026, 1, 19)]
    result = historical_reactions(bars, events, {e: "amc" for e in events})
    assert result["count"] == 3
    assert result["average_abs_move"] is not None
    assert result["max_abs_move"] >= result["median_abs_move"]
    assert all(m["abs_move_pct"] >= 0 for m in result["moves"])


def test_no_bars_or_no_events_is_handled():
    assert historical_reactions([], [dt.date(2026, 1, 1)])["count"] == 0
    assert historical_reactions(_bars({dt.date(2026, 1, 1): 10.0}), [])["count"] == 0
