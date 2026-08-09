"""
News impact tests.

An impact score is the kind of number nobody checks: it looks authoritative,
it sorts a list, and if it were random the list would still look plausible. So
these tests are built around cases where the correct ordering is known by
construction — a big move against a quiet one, a move on volume against the
same move on none — plus the two properties that keep it honest: it must refuse
to score what it cannot measure, and it must not turn that refusal into a zero.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from app.providers.base import Bar, NewsItem
from app.services.news_impact import SymbolContext, band_for, rank, score_item

UTC = dt.timezone.utc


def make_bars(moves: list[float], *, volumes: list[float] | None = None,
              start=dt.date(2026, 6, 1), price=100.0) -> list[Bar]:
    """Bars with exactly the close-to-close returns given."""
    bars, day, c = [], start, price
    for i, m in enumerate(moves):
        c *= (1 + m)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        v = volumes[i] if volumes else 1_000_000.0
        bars.append(Bar(t=dt.datetime.combine(day, dt.time(20, 0), UTC),
                        o=c, h=c * 1.01, l=c * 0.99, c=c, v=v))
        day += dt.timedelta(days=1)
    return bars


def item(published: dt.datetime, ticker="ABC", title="headline") -> NewsItem:
    return NewsItem(id="1", title=title, url="u", published=published,
                    tickers=[ticker])


def quiet_then(move: float, *, volume_multiple: float = 1.0):
    """A calm history followed by one move, and the item that precedes it."""
    moves = [0.002 * (1 if i % 2 else -1) for i in range(60)] + [move]
    vols = [1_000_000.0] * 60 + [1_000_000.0 * volume_multiple]
    bars = make_bars(moves, volumes=vols)
    ctx = SymbolContext.build("ABC", bars)
    # Published the evening before the final bar, so the final bar is the
    # reaction session.
    pub = dt.datetime.combine(bars[-1].t.date() - dt.timedelta(days=1),
                              dt.time(22, 0), UTC)
    return ctx, item(pub)


# --------------------------------------------------------------------------
# The core measurement
# --------------------------------------------------------------------------

def test_a_bigger_move_scores_higher():
    small_ctx, small_item = quiet_then(0.004)
    big_ctx, big_item = quiet_then(0.06)
    small = score_item(small_item, small_ctx)
    big = score_item(big_item, big_ctx)
    assert big["impact"] > small["impact"]
    assert big["components"]["move_sigma"] > small["components"]["move_sigma"]


def test_the_move_is_normalized_by_the_name_s_own_volatility():
    """
    The whole reason the score is a z-score. A 3% day is a shock for a quiet
    name and an ordinary session for a volatile one, and an unnormalized score
    would rank every volatile stock above every calm one permanently.
    """
    calm = SymbolContext.build("ABC", make_bars(
        [0.002 * (1 if i % 2 else -1) for i in range(60)] + [0.03]))
    wild = SymbolContext.build("ABC", make_bars(
        [0.05 * (1 if i % 2 else -1) for i in range(60)] + [0.03]))
    pub = dt.datetime.combine(calm.bars[-1].t.date() - dt.timedelta(days=1),
                              dt.time(22, 0), UTC)
    calm_score = score_item(item(pub), calm)
    wild_score = score_item(item(pub), wild)
    assert calm_score["impact"] > wild_score["impact"]
    assert calm_score["components"]["move_sigma"] > 5
    assert wild_score["components"]["move_sigma"] < 1.5


def test_volume_confirmation_raises_the_score_for_the_same_move():
    quiet_ctx, quiet_item = quiet_then(0.05, volume_multiple=1.0)
    heavy_ctx, heavy_item = quiet_then(0.05, volume_multiple=4.0)
    assert (score_item(heavy_item, heavy_ctx)["impact"]
            > score_item(quiet_item, quiet_ctx)["impact"])


def test_direction_is_reported_and_does_not_change_the_magnitude():
    """
    Impact is about size, not sign — a 5% drop is exactly as consequential as a
    5% rise. Direction is carried separately so the UI can show it without the
    ranking quietly preferring good news.
    """
    up_ctx, up_item = quiet_then(0.05)
    down_ctx, down_item = quiet_then(-0.05)
    up = score_item(up_item, up_ctx)
    down = score_item(down_item, down_ctx)
    assert up["direction"] == "up" and down["direction"] == "down"
    assert up["impact"] == pytest.approx(down["impact"], abs=1.0)


def test_coverage_clustering_raises_the_score():
    ctx, it = quiet_then(0.02)
    alone = score_item(it, ctx, coverage=1)
    crowded = score_item(it, ctx, coverage=8)
    assert crowded["impact"] > alone["impact"]
    assert crowded["components"]["coverage_items_24h"] == 8


def test_earnings_proximity_contributes_only_near_the_date():
    bars = make_bars([0.003] * 60 + [0.02])
    last = bars[-1].t.date()
    pub = dt.datetime.combine(last - dt.timedelta(days=1), dt.time(22, 0), UTC)

    near = SymbolContext.build("ABC", bars, next_earnings=last + dt.timedelta(days=2))
    far = SymbolContext.build("ABC", bars, next_earnings=last + dt.timedelta(days=200))
    n = score_item(item(pub), near, today=last)
    f = score_item(item(pub), far, today=last)
    assert n["components"]["event_score"] > 0
    assert f["components"]["event_score"] == 0
    assert n["impact"] > f["impact"]


# --------------------------------------------------------------------------
# Refusing to score, which is the honest half
# --------------------------------------------------------------------------

def test_an_item_with_no_completed_session_is_unscored_not_zero():
    """
    The failure this prevents: a brand-new headline gets impact 0, sorts to the
    bottom next to genuinely inconsequential items, and the reader concludes it
    was measured and found unimportant. Absence of evidence must not render as
    evidence of absence.
    """
    bars = make_bars([0.004] * 60)
    ctx = SymbolContext.build("ABC", bars)
    future = dt.datetime.combine(bars[-1].t.date() + dt.timedelta(days=3),
                                 dt.time(13, 0), UTC)
    out = score_item(item(future), ctx)
    assert out["impact"] is None
    assert out["band"] == "unscored"
    assert "not a low score" in out["reason"]


def test_no_price_history_is_unscored():
    ctx = SymbolContext.build("ABC", make_bars([0.01] * 3))
    out = score_item(item(dt.datetime(2026, 6, 2, 12, tzinfo=UTC)), ctx)
    assert out["impact"] is None


def test_unscored_items_sort_last_not_first():
    bars = make_bars([0.002] * 60 + [0.05])
    ctx = SymbolContext.build("ABC", bars)
    last = bars[-1].t.date()
    scored = item(dt.datetime.combine(last - dt.timedelta(days=1), dt.time(22), UTC),
                  title="measured")
    unscored = item(dt.datetime.combine(last + dt.timedelta(days=4), dt.time(12), UTC),
                    title="too new")
    out = rank([unscored, scored], {"ABC": ctx})
    assert out["items"][0]["title"] == "measured"
    assert out["items"][-1]["title"] == "too new"
    assert out["items"][-1]["scoring"]["impact"] is None


# --------------------------------------------------------------------------
# Window selection
# --------------------------------------------------------------------------

def test_an_item_published_after_the_close_is_measured_against_the_next_session():
    """
    Getting this backwards would credit a headline with a move that finished
    before it was written — the classic way a score like this becomes a lagging
    indicator wearing a leading indicator's clothes.
    """
    bars = make_bars([0.002] * 60 + [0.04, 0.01])
    ctx = SymbolContext.build("ABC", bars)
    after_close = dt.datetime.combine(bars[-2].t.date(), dt.time(23, 0), UTC)
    out = score_item(item(after_close), ctx)
    assert out["window"]["reaction_close"] == bars[-1].t.date().isoformat()


def test_an_item_published_during_the_session_is_measured_against_that_session():
    bars = make_bars([0.002] * 60 + [0.04, 0.01])
    ctx = SymbolContext.build("ABC", bars)
    intraday = dt.datetime.combine(bars[-2].t.date(), dt.time(14, 0), UTC)
    out = score_item(item(intraday), ctx)
    assert out["window"]["reaction_close"] == bars[-2].t.date().isoformat()


# --------------------------------------------------------------------------
# Presentation contracts
# --------------------------------------------------------------------------

def test_bands_are_monotonic_in_the_score():
    order = ["low", "moderate", "elevated", "high"]
    seen = [band_for(s) for s in range(0, 101, 5)]
    idx = [order.index(b) for b in seen]
    assert idx == sorted(idx), f"band thresholds are not monotonic: {seen}"


def test_score_is_bounded():
    ctx, it = quiet_then(0.60, volume_multiple=50)
    out = score_item(it, ctx, coverage=100)
    assert 0.0 <= out["impact"] <= 100.0


def test_every_scored_item_carries_its_caveat_and_window():
    ctx, it = quiet_then(0.03)
    out = score_item(it, ctx)
    assert "not causation" in out["caveat"]
    assert set(out["window"]) == {"published", "prior_close", "reaction_close"}


def test_rank_reports_its_method_and_its_limits():
    ctx, it = quiet_then(0.03)
    out = rank([it], {"ABC": ctx})
    assert "not inferred from the headline text" in out["method"]
    assert "co-movement" in out["limits"]


def test_components_reconstruct_the_total():
    """
    The score must be exactly the weighted sum it claims to be. If the reported
    components stopped adding up to the reported total, the transparency would
    be decorative — worse than not showing components at all.
    """
    from app.services.news_impact import W_COVERAGE, W_EVENT, W_MOVE, W_VOLUME
    ctx, it = quiet_then(0.04, volume_multiple=2.5)
    out = score_item(it, ctx, coverage=4)
    c = out["components"]
    expected = 100 * (W_MOVE * c["move_score"] + W_VOLUME * c["volume_score"]
                      + W_COVERAGE * c["coverage_score"] + W_EVENT * c["event_score"])
    assert out["impact"] == pytest.approx(expected, abs=0.15)


def test_scores_are_comparable_across_symbols():
    """
    Two names with different volatility and the same z-score must land in the
    same band. Otherwise the feed silently ranks by volatility.
    """
    calm = SymbolContext.build("ABC", make_bars([0.002] * 60 + [0.012]))
    wild = SymbolContext.build("XYZ", make_bars([0.02] * 60 + [0.12]))
    pub_c = dt.datetime.combine(calm.bars[-1].t.date() - dt.timedelta(days=1),
                                dt.time(22), UTC)
    a = score_item(item(pub_c), calm)
    b = score_item(item(pub_c, ticker="XYZ"), wild)
    assert a["components"]["move_sigma"] == pytest.approx(
        b["components"]["move_sigma"], rel=0.35)
    assert a["band"] == b["band"]
