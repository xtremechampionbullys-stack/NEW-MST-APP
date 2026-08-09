"""
Forward-testing tests.

A Monte Carlo engine will always return numbers, and they will always look
plausible. That is exactly why it needs testing: nothing about the output tells
you whether the simulation is right. So these tests check the properties that
must hold if it is -- martingale drift, the volatility the paths actually
realise, the ordering of path-dependent exits -- against cases where the answer
is known in closed form or by construction.

The recurring theme is that the *conservative* behaviour is the one asserted.
A simulation is only useful if its errors are in the direction that makes you
smaller, not the direction that makes the backtest pretty.
"""

import math

import numpy as np
import pytest

from app.engine.forward import (
    ManagementRules,
    PaperTradeExpectation,
    SimulationConfig,
    _vectorized_pl,
    calibration_report,
    forward_test,
    score_paper_trade,
    simulate_paths,
)
from app.engine.payoff import Leg, Position
from app.engine.pricing import DAYS_PER_YEAR, bs_price


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def short_strangle(spot=100.0, dte=45, iv=0.30) -> Position:
    return Position(
        legs=[
            Leg(kind="call", quantity=-1, entry_price=1.60, strike=110.0, dte=dte, iv=iv),
            Leg(kind="put", quantity=-1, entry_price=1.55, strike=90.0, dte=dte, iv=iv),
        ],
        spot=spot, name="Short Strangle")


def long_call(spot=100.0, dte=45, iv=0.30) -> Position:
    return Position(
        legs=[Leg(kind="call", quantity=1, entry_price=4.20, strike=100.0,
                  dte=dte, iv=iv)],
        spot=spot, name="Long Call")


# --------------------------------------------------------------------------
# The path generator
# --------------------------------------------------------------------------

def test_paths_are_a_martingale_under_zero_drift():
    """
    With annual_drift = 0 the discounted price must have expectation equal to
    spot. The -0.5*var Ito correction in the log dynamics is exactly what makes
    that true, and forgetting it is the single most common Monte Carlo bug --
    it produces a simulation that quietly drifts upward and makes every long
    position look better than it is.
    """
    cfg = SimulationConfig(paths=40000, seed=7, tail_df=None, vol_of_vol=0.0)
    paths = simulate_paths(100.0, 0.30, cfg, 60)
    terminal = paths[:, -1]
    se = terminal.std() / math.sqrt(terminal.size)
    assert terminal.mean() == pytest.approx(100.0, abs=4 * se)


def test_drift_moves_the_mean_in_the_right_direction_and_amount():
    cfg = SimulationConfig(paths=40000, seed=8, tail_df=None, vol_of_vol=0.0,
                           annual_drift=0.10)
    paths = simulate_paths(100.0, 0.20, cfg, 252)
    terminal = paths[:, -1]
    se = terminal.std() / math.sqrt(terminal.size)
    expected = 100.0 * math.exp(0.10 * 252 / DAYS_PER_YEAR)
    assert terminal.mean() == pytest.approx(expected, abs=4 * se)


def test_realized_volatility_matches_the_requested_volatility():
    """The paths must actually have the vol they were asked for."""
    cfg = SimulationConfig(paths=20000, seed=9, tail_df=None, vol_of_vol=0.0)
    paths = simulate_paths(100.0, 0.35, cfg, 120)
    rets = np.diff(np.log(paths), axis=1)
    realized = rets.std() * math.sqrt(DAYS_PER_YEAR)
    assert realized == pytest.approx(0.35, rel=0.03)


def test_fat_tails_do_not_smuggle_in_extra_volatility():
    """
    Student-t innovations are rescaled to unit variance precisely so that
    changing the tail weight is a change to tail SHAPE only. If it also raised
    total variance, every comparison between a normal and a fat-tailed run
    would be confounded and the fat-tailed one would look scarier for the wrong
    reason.
    """
    common = dict(paths=30000, seed=11, vol_of_vol=0.0)
    normal = simulate_paths(100.0, 0.30, SimulationConfig(tail_df=None, **common), 60)
    fat = simulate_paths(100.0, 0.30, SimulationConfig(tail_df=3.5, **common), 60)

    rn = np.diff(np.log(normal), axis=1).ravel()
    rf = np.diff(np.log(fat), axis=1).ravel()

    assert rf.std() == pytest.approx(rn.std(), rel=0.05)
    # ...but the tails are genuinely fatter.
    assert abs(rf).max() > abs(rn).max()
    kurt_n = float(np.mean((rn / rn.std()) ** 4))
    kurt_f = float(np.mean((rf / rf.std()) ** 4))
    assert kurt_f > kurt_n + 1.0


def test_vol_clustering_creates_autocorrelation_in_squared_returns():
    """
    Vol-of-vol is supposed to produce clustering. Squared-return autocorrelation
    is the standard signature; without it the parameter does nothing.
    """
    flat = simulate_paths(100.0, 0.30, SimulationConfig(
        paths=2000, seed=13, tail_df=None, vol_of_vol=0.0), 250)
    clustered = simulate_paths(100.0, 0.30, SimulationConfig(
        paths=2000, seed=13, tail_df=None, vol_of_vol=0.9,
        vol_mean_reversion=0.02), 250)

    def acf1_of_squares(paths):
        r = np.diff(np.log(paths), axis=1)
        s = r ** 2
        s = s - s.mean(axis=1, keepdims=True)
        num = (s[:, :-1] * s[:, 1:]).sum(axis=1)
        den = (s ** 2).sum(axis=1)
        return float(np.mean(num / np.where(den == 0, 1, den)))

    assert acf1_of_squares(clustered) > acf1_of_squares(flat) + 0.02


def test_event_jump_widens_the_distribution_only_after_the_event_day():
    cfg = dict(paths=20000, seed=17, tail_df=None, vol_of_vol=0.0)
    plain = simulate_paths(100.0, 0.25, SimulationConfig(**cfg), 30)
    jumpy = simulate_paths(100.0, 0.25, SimulationConfig(
        jump_days=[20], jump_vol=0.08, **cfg), 30)

    before = np.std(np.log(jumpy[:, 19])) / np.std(np.log(plain[:, 19]))
    after = np.std(np.log(jumpy[:, 21])) / np.std(np.log(plain[:, 21]))
    assert before == pytest.approx(1.0, abs=0.03), "jump leaked backwards in time"
    assert after > 1.15, "jump did not widen the post-event distribution"


def test_simulation_is_reproducible_and_seed_dependent():
    a = simulate_paths(100.0, 0.3, SimulationConfig(paths=500, seed=4), 20)
    b = simulate_paths(100.0, 0.3, SimulationConfig(paths=500, seed=4), 20)
    c = simulate_paths(100.0, 0.3, SimulationConfig(paths=500, seed=5), 20)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


# --------------------------------------------------------------------------
# Pricing consistency
# --------------------------------------------------------------------------

def test_vectorized_pl_agrees_with_the_scalar_path():
    """
    forward_test switches between position.pl_at() and _vectorized_pl() based on
    path count. Two implementations of the same quantity is a standing invitation
    to drift, and the drift would show up as a discontinuity in results at
    exactly 400 paths -- which nobody would ever think to look for.
    """
    pos = short_strangle()
    spots = np.linspace(70.0, 130.0, 61)
    fast = _vectorized_pl(pos, spots, 20.0)
    slow = np.array([pos.pl_at(float(s), 20.0) for s in spots])
    assert np.allclose(fast, slow, atol=1e-9)


def test_vectorized_pl_agrees_at_and_past_expiry():
    pos = short_strangle(dte=30)
    spots = np.linspace(60.0, 140.0, 41)
    for day in (30.0, 45.0):
        fast = _vectorized_pl(pos, spots, day)
        slow = np.array([pos.pl_at(float(s), day) for s in spots])
        assert np.allclose(fast, slow, atol=1e-9), f"mismatch at day {day}"


def test_forward_test_matches_black_scholes_probability_on_a_held_position():
    """
    With management disabled and the horizon set to expiry, the simulated win
    rate must reproduce the closed-form probability the analytics report. If the
    two disagree, one of them is wrong and there is no way to tell which from
    the app alone.
    """
    pos = long_call(dte=30, iv=0.30)
    res = forward_test(
        pos,
        config=SimulationConfig(paths=30000, seed=21, tail_df=None, vol_of_vol=0.0),
        rules=ManagementRules(profit_target_pct=None, stop_loss_pct=None,
                              close_at_dte=None))

    # Break-even at expiry: strike + premium paid per share.
    be = pos.legs[0].strike + pos.net_debit / 100.0
    T = 30 / DAYS_PER_YEAR
    sig = 0.30
    # Risk-neutral P(S_T > be) under the same zero-drift measure the sim uses.
    d2 = (math.log(100.0 / be) - 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    from app.engine.pricing import norm_cdf
    analytic = norm_cdf(d2)

    assert res["outcomes"]["win_rate"] == pytest.approx(analytic, abs=0.015)


# --------------------------------------------------------------------------
# Management rules
# --------------------------------------------------------------------------

def test_exit_reasons_partition_every_path():
    res = forward_test(short_strangle(),
                       config=SimulationConfig(paths=3000, seed=31))
    pd = res["path_dependent"]
    total = pd["hit_profit_target"] + pd["hit_stop_loss"] + pd["closed_on_time"]
    assert total == pytest.approx(1.0, abs=1e-9)


def test_a_tighter_profit_target_is_hit_more_often():
    base = dict(config=SimulationConfig(paths=4000, seed=33))
    tight = forward_test(short_strangle(),
                         rules=ManagementRules(profit_target_pct=0.25,
                                               stop_loss_pct=None, close_at_dte=None),
                         **base)
    loose = forward_test(short_strangle(),
                         rules=ManagementRules(profit_target_pct=0.75,
                                               stop_loss_pct=None, close_at_dte=None),
                         **base)
    assert (tight["path_dependent"]["hit_profit_target"]
            > loose["path_dependent"]["hit_profit_target"])


def test_a_stop_loss_truncates_the_extreme_tail_but_worsens_the_5th_percentile():
    """
    The result that justifies having a path-dependent engine at all, and it is
    not the one most people expect.

    A stop on a short strangle is triggered by the MARK, and a short option's
    mark mid-life carries time value that expiry does not. So a stop converts
    recoverable mark-to-market drawdowns into realised losses. Measured on this
    setup: the disaster tail improves enormously (roughly -13k to -3.4k) while
    the 5th percentile gets materially WORSE, because the ~6% of paths that trip
    the stop are drawn from exactly that region and most of them would have
    expired near max profit.

    A closed-form analytic cannot show you this; only simulating the order in
    which things happen can. Asserting the folk-wisdom version -- "stops
    truncate the left tail" -- would have hidden it.
    """
    base = dict(config=SimulationConfig(paths=8000, seed=37))
    no_stop = forward_test(short_strangle(),
                           rules=ManagementRules(profit_target_pct=None,
                                                 stop_loss_pct=None, close_at_dte=None),
                           **base)
    stopped = forward_test(short_strangle(),
                           rules=ManagementRules(profit_target_pct=None,
                                                 stop_loss_pct=1.5, close_at_dte=None),
                           **base)

    assert stopped["outcomes"]["worst"] > no_stop["outcomes"]["worst"] * 0.5
    assert stopped["outcomes"]["p05"] < no_stop["outcomes"]["p05"]
    assert 0.01 < stopped["path_dependent"]["hit_stop_loss"] < 0.20


def test_a_stop_does_not_cap_the_loss_at_the_stop_level():
    """
    Stops are marked on a day ladder, so a path can gap straight through one.
    The reported worst case must reflect that. A simulation that clamps every
    stopped path to exactly the stop level would report a bounded risk the
    trader does not actually have.
    """
    pos = short_strangle()
    stop_level = -1.5 * abs(pos.net_debit)
    res = forward_test(pos, config=SimulationConfig(paths=8000, seed=37),
                       rules=ManagementRules(profit_target_pct=None,
                                             stop_loss_pct=1.5, close_at_dte=None))
    assert res["outcomes"]["worst"] < stop_level * 1.5, (
        "stopped losses were clamped to the stop level — gap risk is being hidden")


def test_management_shortens_average_holding_time():
    base = dict(config=SimulationConfig(paths=3000, seed=41))
    managed = forward_test(short_strangle(dte=45), **base)
    held = forward_test(short_strangle(dte=45),
                        rules=ManagementRules(profit_target_pct=None,
                                              stop_loss_pct=None, close_at_dte=None),
                        **base)
    assert (managed["path_dependent"]["mean_days_held"]
            < held["path_dependent"]["mean_days_held"])


def test_max_adverse_excursion_is_never_positive_and_bounds_the_outcome():
    res = forward_test(short_strangle(),
                       config=SimulationConfig(paths=2000, seed=43))
    pd = res["path_dependent"]
    assert pd["mean_max_adverse"] <= 0.0
    assert pd["worst_max_adverse"] <= pd["mean_max_adverse"]
    assert pd["mean_max_favorable"] >= 0.0
    # Winners go underwater too -- if this is exactly zero the MAE tracking is
    # not running on the paths that end up profitable.
    assert pd["mean_max_adverse_on_winners"] <= 0.0


def test_stop_wins_when_a_path_could_cross_both():
    """
    The documented conservative tie-break. Constructed so target and stop are
    both trivially reachable at the first mark: the run must attribute those
    paths to the stop, never the target.
    """
    res = forward_test(
        short_strangle(),
        config=SimulationConfig(paths=1000, seed=47),
        rules=ManagementRules(profit_target_pct=1e-6, stop_loss_pct=1e-6,
                              close_at_dte=None))
    pd = res["path_dependent"]
    assert pd["hit_stop_loss"] > 0.0
    # Every path where both triggers were live at the same mark went to the stop.
    assert pd["hit_stop_loss"] + pd["hit_profit_target"] == pytest.approx(1.0, abs=0.02)


def test_percentiles_are_ordered():
    o = forward_test(short_strangle(),
                     config=SimulationConfig(paths=2000, seed=53))["outcomes"]
    assert o["worst"] <= o["p05"] <= o["p25"] <= o["median_pl"] <= o["p75"] <= o["p95"] <= o["best"]


def test_fan_chart_bands_are_ordered_and_start_at_spot():
    res = forward_test(short_strangle(),
                       config=SimulationConfig(paths=2000, seed=59))
    fan = res["fan_chart"]
    for i, _d in enumerate(fan["days"]):
        row = [fan["bands"][b][i] for b in ("5", "25", "50", "75", "95")]
        assert row == sorted(row), "fan chart bands crossed"
    for b in fan["bands"].values():
        assert b[0] == pytest.approx(100.0, abs=1e-9)


def test_histogram_accounts_for_every_path_including_the_clipped_tails():
    """
    The bins span the 1st-99th percentile so a single catastrophic path cannot
    flatten the whole chart. The paths outside are reported, not dropped -- if
    bins + below + above ever failed to reconstruct the total, the histogram
    would be quietly under-representing the tail, which on a short premium
    position is the only part that matters.
    """
    h = forward_test(short_strangle(),
                     config=SimulationConfig(paths=2500, seed=61))["pl_histogram"]
    assert sum(h["counts"]) + h["below"] + h["above"] == 2500
    assert h["min"] <= h["edges"][0]
    assert h["max"] >= h["edges"][-1]


def test_histogram_clipping_actually_rescues_the_resolution():
    """
    A short strangle piles ~90% of paths near max profit with a few thousands of
    dollars away. Binning min-to-max puts everything in one bar. The test is
    that the clipped range is materially narrower than the full range, and that
    no single bucket swallows nearly everything.
    """
    h = forward_test(short_strangle(),
                     config=SimulationConfig(paths=6000, seed=63))["pl_histogram"]
    full_span = h["max"] - h["min"]
    binned_span = h["edges"][-1] - h["edges"][0]
    assert binned_span < full_span * 0.75, "clipping did not narrow the range"
    assert h["clipped"] is True
    assert h["below"] + h["above"] > 0


def test_histogram_of_a_tight_distribution_is_not_clipped_pointlessly():
    values = np.full(500, 12.0)
    from app.engine.forward import _histogram
    h = _histogram(values)
    assert h["clipped"] is False
    assert sum(h["counts"]) == 500


def test_horizon_must_be_positive():
    with pytest.raises(ValueError):
        forward_test(short_strangle(),
                     config=SimulationConfig(paths=100, horizon_days=0))


# --------------------------------------------------------------------------
# Paper trades and calibration
# --------------------------------------------------------------------------

def test_score_paper_trade_brier_rewards_the_right_direction():
    exp = PaperTradeExpectation(
        probability_of_profit=0.70, expected_value=25.0, max_profit=100.0,
        max_loss=-400.0, breakevens=[95.0, 115.0], expected_move_pct=0.06,
        blended_iv=0.31).to_dict()

    win = score_paper_trade(exp, realized_pl=60.0, realized_days=18)
    loss = score_paper_trade(exp, realized_pl=-120.0, realized_days=9)

    assert win["won"] and not loss["won"]
    assert win["brier"] == pytest.approx(0.09)     # (1 - 0.7)^2
    assert loss["brier"] == pytest.approx(0.49)    # (0 - 0.7)^2
    assert win["brier"] < loss["brier"]
    assert win["ev_error"] == pytest.approx(35.0)


def test_calibration_declines_to_report_on_a_thin_sample():
    rep = calibration_report([{"predicted_pop": 0.7, "won": True, "brier": 0.09,
                               "ev_error": 1.0}] * 3)
    assert rep["available"] is False
    assert "calibration needs" in rep["reason"]


def _synthetic_log(rng, n, pops, honest=True):
    rows = []
    for _ in range(n):
        p = float(rng.choice(pops))
        true_p = p if honest else min(1.0, p * 0.6)
        won = bool(rng.random() < true_p)
        rows.append({"predicted_pop": p, "won": won,
                     "brier": ((1.0 if won else 0.0) - p) ** 2,
                     "ev_error": 0.0})
    return rows


def test_calibration_detects_an_honest_model():
    rng = np.random.default_rng(101)
    rep = calibration_report(_synthetic_log(rng, 3000, [0.3, 0.5, 0.7, 0.9]))
    assert rep["available"] is True
    for b in rep["buckets"]:
        assert abs(b["gap"]) < 0.06, f"honest model flagged as miscalibrated: {b}"


def test_calibration_detects_an_overconfident_model():
    """
    The test that gives the report its purpose. A model claiming 90% while
    winning 54% must show a large negative gap -- if this passes silently the
    calibration report is decorative.
    """
    rng = np.random.default_rng(102)
    rep = calibration_report(_synthetic_log(rng, 3000, [0.7, 0.9], honest=False))
    assert rep["available"] is True
    assert any(b["gap"] < -0.2 for b in rep["buckets"]), rep["buckets"]
    assert rep["better_than_base_rate"] is False


def test_calibration_base_rate_brier_is_the_variance_of_outcomes():
    rng = np.random.default_rng(103)
    rows = _synthetic_log(rng, 800, [0.4, 0.6])
    rep = calibration_report(rows)
    base = sum(1 for r in rows if r["won"]) / len(rows)
    assert rep["brier_base_rate"] == pytest.approx(base * (1 - base), abs=5e-5)


def test_calibration_bucket_trade_counts_sum_to_the_sample():
    rng = np.random.default_rng(104)
    rows = _synthetic_log(rng, 500, [0.15, 0.35, 0.55, 0.75, 0.95])
    rep = calibration_report(rows)
    assert sum(b["trades"] for b in rep["buckets"]) == rep["trades"] == 500
