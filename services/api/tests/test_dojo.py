"""
Dojo tests.

The dojo searches a rule space and reports the winner. Anything that does that
will produce an impressive-looking result on pure noise -- that is not a risk,
it is a mathematical certainty. So the tests here are not "does it find good
rules". They are:

  1. Is the out-of-sample window genuinely out of sample?
  2. Does it say "no skill" when there is no skill?
  3. Does the score it optimises resist the obvious ways to game it?

A dojo that passes these is trustworthy when it says no. A dojo that fails any
of them is a machine for generating confident nonsense.
"""

import datetime as dt
import math

import numpy as np
import pytest

from app.engine.backtest import BacktestRules
from app.engine.dojo import SearchSpace, _apply, _objective, run_dojo
from app.providers.base import Bar


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_bars(n=700, seed=3, vol=0.018, drift=0.0002, start=100.0):
    """Daily bars from a plain random walk -- by construction, no edge exists."""
    rng = np.random.default_rng(seed)
    price = start
    bars, day = [], dt.date(2021, 1, 4)
    for _ in range(n):
        r = rng.normal(drift, vol)
        o = price
        price *= math.exp(r)
        h = max(o, price) * math.exp(abs(rng.normal(0, vol * 0.5)))
        l = min(o, price) * math.exp(-abs(rng.normal(0, vol * 0.5)))
        bars.append(Bar(t=dt.datetime.combine(day, dt.time(20, 0), dt.timezone.utc),
                        o=o, h=h, l=l, c=price, v=1e6))
        day += dt.timedelta(days=1)
    return bars


BASE = BacktestRules(template_id="short_strangle", params={"delta": 0.16},
                     entry_every_days=7, target_dte=45)


# --------------------------------------------------------------------------
# The leak test -- the reason this file exists
# --------------------------------------------------------------------------

def test_train_and_test_windows_do_not_overlap():
    """
    The bug this catches is invisible in the output: if the test slice carries a
    warm-up prefix longer than the backtester's own warm-up, the test window
    starts TRADING before the training window ends, and training data leaks into
    the "out-of-sample" score. Everything still runs. Every number still looks
    fine. The validation is simply a lie.

    Reported windows are trading windows, so they can be compared directly.
    """
    res = run_dojo(make_bars(700), BASE, variants=6, seed=1)
    train_end = dt.date.fromisoformat(res["train_window"]["end"])
    test_start = dt.date.fromisoformat(res["test_window"]["start"])
    assert test_start >= train_end, (
        f"out-of-sample window begins {test_start}, before training ends {train_end}")


def test_reported_bar_counts_exclude_the_untraded_warmup():
    res = run_dojo(make_bars(700), BASE, variants=5, seed=1)
    total = res["train_window"]["bars"] + res["test_window"]["bars"]
    # Both windows drop a 60-bar warm-up, and the test slice re-uses the tail of
    # the training slice as its warm-up, so the traded bars must come to fewer
    # than the raw history.
    assert total < 700
    assert res["train_window"]["bars"] > 0
    assert res["test_window"]["bars"] > 0


def test_windows_are_contiguous_in_time():
    res = run_dojo(make_bars(700), BASE, variants=5, seed=1)
    for w in ("train_window", "test_window"):
        start = dt.date.fromisoformat(res[w]["start"])
        end = dt.date.fromisoformat(res[w]["end"])
        assert start < end


# --------------------------------------------------------------------------
# Honesty on noise
# --------------------------------------------------------------------------

def test_selection_skill_is_reported_as_a_percentile_and_is_not_flattering():
    """
    On a random walk there is nothing to select. The percentile is allowed to
    land anywhere by chance, but the VERDICT must follow the percentile rather
    than the leaderboard -- and it must not congratulate itself.
    """
    res = run_dojo(make_bars(700, seed=5), BASE, variants=12, seed=2)
    v = res["validation"]
    assert 0.0 <= v["out_of_sample_percentile"] <= 100.0
    assert v["selection_beat_random"] == (v["out_of_sample_percentile"] > 60)
    if not v["selection_beat_random"]:
        assert "No selection skill" in res["verdict"]
        assert "Do not deploy" in res["verdict"]
    assert "the same price history" in res["caveat"]


def test_verdict_always_states_how_many_variants_were_tried():
    res = run_dojo(make_bars(700), BASE, variants=10, seed=3)
    assert str(res["variants_tried"]) in res["verdict"] + res["caveat"]
    assert res["variants_tried"] <= 10
    assert res["search_space_size"] > res["variants_tried"]


def test_a_baseline_is_scored_on_the_same_window():
    """
    Without this, a search that is strictly worse than the rules you started
    with still reports a winner.
    """
    res = run_dojo(make_bars(700), BASE, variants=8, seed=4)
    assert res["baseline"]["test_metrics"] is not None
    assert res["baseline"]["rules"]["target_dte"] == BASE.target_dte
    assert isinstance(res["validation"]["selection_beat_baseline"], bool)


def test_selected_variant_is_the_train_leader_not_the_test_leader():
    """
    The selection must be made on training data only. If the code ever ranked on
    test_score, the headline would be the best OUT-OF-SAMPLE result out of N --
    which is the exact overfitting the module exists to prevent, wearing the
    costume of validation.
    """
    res = run_dojo(make_bars(700), BASE, variants=15, seed=6)
    board = res["leaderboard"]
    assert board[0]["train_score"] == max(r["train_score"] for r in board)
    assert res["selected"]["train_score"] == pytest.approx(board[0]["train_score"],
                                                           abs=1e-3)
    # And the leaderboard is ordered by train score, not test score.
    assert board == sorted(board, key=lambda r: r["train_score"], reverse=True)


def test_degradation_is_train_minus_test():
    res = run_dojo(make_bars(700), BASE, variants=8, seed=7)
    s = res["selected"]
    assert res["validation"]["degradation"] == pytest.approx(
        s["train_score"] - s["test_score"], abs=2e-3)


def test_results_are_deterministic_for_a_seed():
    a = run_dojo(make_bars(700), BASE, variants=8, seed=11)
    b = run_dojo(make_bars(700), BASE, variants=8, seed=11)
    assert a["selected"]["variant"] == b["selected"]["variant"]
    assert a["validation"] == b["validation"]


# --------------------------------------------------------------------------
# The objective function
# --------------------------------------------------------------------------

def test_objective_rejects_thin_samples_outright():
    """A variant with four trades has no measurable performance, whatever it made."""
    lucky = {"trades": 4, "total_pl": 99999.0, "max_drawdown": -1.0, "sharpe": 9.0}
    assert _objective(lucky) < -1e8


def test_objective_prefers_the_same_return_at_lower_drawdown():
    calm = {"trades": 80, "total_pl": 5000.0, "max_drawdown": -1000.0, "sharpe": 1.0}
    wild = {"trades": 80, "total_pl": 5000.0, "max_drawdown": -4000.0, "sharpe": 1.0}
    assert _objective(calm) > _objective(wild)


def test_objective_shrinks_toward_zero_on_small_samples():
    """
    Two variants with identical risk-adjusted returns are not equally credible
    if one got there on 12 trades and the other on 200.
    """
    few = {"trades": 12, "total_pl": 3000.0, "max_drawdown": -1000.0, "sharpe": 1.5}
    many = {"trades": 200, "total_pl": 3000.0, "max_drawdown": -1000.0, "sharpe": 1.5}
    assert _objective(many) > _objective(few) > 0


def test_objective_is_not_raw_profit():
    """
    The whole point of not maximising P/L: the variant that made more money by
    taking far more risk must not win.
    """
    huge_pl_huge_risk = {"trades": 60, "total_pl": 20000.0,
                         "max_drawdown": -30000.0, "sharpe": 0.2}
    modest_pl_low_risk = {"trades": 60, "total_pl": 4000.0,
                          "max_drawdown": -1200.0, "sharpe": 1.4}
    assert _objective(modest_pl_low_risk) > _objective(huge_pl_huge_risk)


def test_objective_handles_a_zero_drawdown_without_exploding():
    assert math.isfinite(_objective(
        {"trades": 40, "total_pl": 100.0, "max_drawdown": 0.0, "sharpe": None}))


# --------------------------------------------------------------------------
# Search space plumbing
# --------------------------------------------------------------------------

def test_apply_maps_every_searched_dimension_onto_the_rules():
    """
    A dimension that is searched but not actually applied would make the dojo
    report variation it never tested -- silently averaging over nothing.
    """
    v = {"profit_target_pct": 0.35, "stop_loss_pct": 3.0, "close_at_dte": 14,
         "target_dte": 60, "delta": 0.30, "entry_every_days": 14}
    r = _apply(BASE, v)
    assert r.profit_target_pct == 0.35
    assert r.stop_loss_pct == 3.0
    assert r.close_at_dte == 14
    assert r.target_dte == 60
    assert r.params["delta"] == 0.30
    assert r.entry_every_days == 14
    # Untouched fields survive.
    assert r.template_id == BASE.template_id
    assert r.commission_per_contract == BASE.commission_per_contract


def test_zero_means_disabled_not_zero():
    """0 in the search space is the 'no stop / no time exit' arm, not a stop at 0."""
    r = _apply(BASE, {"stop_loss_pct": 0.0, "close_at_dte": 0})
    assert r.stop_loss_pct is None
    assert r.close_at_dte is None


def test_search_space_size_is_the_product_of_its_dimensions():
    s = SearchSpace()
    expected = (len(s.profit_target_pct) * len(s.stop_loss_pct)
                * len(s.close_at_dte) * len(s.target_dte)
                * len(s.delta) * len(s.entry_every_days))
    assert s.size() == expected


def test_variants_are_deduplicated():
    tiny = SearchSpace(profit_target_pct=[0.5], stop_loss_pct=[2.0],
                       close_at_dte=[21], target_dte=[45], delta=[0.16],
                       entry_every_days=[7])
    assert tiny.size() == 1
    res = run_dojo(make_bars(700), BASE, space=tiny, variants=10, seed=9)
    assert res["variants_tried"] == 1, "the same variant was scored more than once"


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

def test_short_history_is_refused_rather_than_silently_split():
    with pytest.raises(ValueError, match="at least 300 bars"):
        run_dojo(make_bars(200), BASE, variants=4)


def test_insufficient_out_of_sample_window_is_refused():
    with pytest.raises(ValueError, match="out-of-sample"):
        run_dojo(make_bars(340), BASE, variants=4, train_fraction=0.9)
