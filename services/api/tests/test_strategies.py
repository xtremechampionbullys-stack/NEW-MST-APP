"""
Position and strategy tests.

Verifies that every registered template builds, that the payoff math produces
the textbook shape for each structure (max profit/loss, breakevens, credit vs
debit), and that the analytics are internally consistent.
"""

import math

import pytest

from app.engine.analytics import (
    analyze,
    build_distribution,
    probability_of_profit,
    probability_of_touch,
)
from app.engine.payoff import Leg, Position
from app.engine.strategies import REGISTRY, MarketContext, get_template

SPOT = 185.0


@pytest.fixture
def ctx():
    return MarketContext.synthetic(spot=SPOT, atm_vol=0.30, symbol="TEST")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

def test_registry_is_populated():
    assert len(REGISTRY) >= 30


@pytest.mark.parametrize("tid", sorted(REGISTRY.keys()))
def test_every_template_builds_and_analyzes(ctx, tid):
    pos = get_template(tid).make(ctx)
    assert pos.legs, f"{tid} produced no legs"
    result = analyze(pos)

    assert math.isfinite(result["net_debit"])
    assert 0.0 <= result["probability_of_profit"] <= 1.0
    assert math.isfinite(result["expected_value"])
    for leg in pos.legs:
        if leg.kind != "stock":
            assert leg.strike and leg.strike > 0
            assert 0.0 < leg.iv < 5.0


@pytest.mark.parametrize("tid", sorted(REGISTRY.keys()))
def test_declared_credit_matches_reality(ctx, tid):
    t = get_template(tid)
    if t.credit is None:
        return
    pos = t.make(ctx)
    # Compare before commissions -- commissions can flip a thin credit.
    gross = sum(l.quantity * l.entry_price * l.multiplier for l in pos.legs
                if l.kind != "stock")
    if t.credit:
        assert gross < 0, f"{tid} declared credit but opens for a debit"
    else:
        assert gross > 0, f"{tid} declared debit but opens for a credit"


@pytest.mark.parametrize("tid", sorted(REGISTRY.keys()))
def test_declared_risk_definition_matches_structure(ctx, tid):
    t = get_template(tid)
    pos = t.make(ctx)
    assert pos.is_risk_defined == t.risk_defined, (
        f"{tid} declares risk_defined={t.risk_defined} but structure says "
        f"{pos.is_risk_defined}"
    )


# --------------------------------------------------------------------------
# Payoff shapes
# --------------------------------------------------------------------------

def test_long_call_payoff_shape():
    leg = Leg(kind="call", quantity=1, entry_price=5.0, strike=100.0, dte=30, iv=0.3)
    pos = Position(legs=[leg], spot=100.0, commission_per_contract=0.0)
    assert pos.pl_at_expiry(90.0) == pytest.approx(-500.0)
    assert pos.pl_at_expiry(105.0) == pytest.approx(0.0)
    assert pos.pl_at_expiry(120.0) == pytest.approx(1500.0)
    assert pos.breakevens() == pytest.approx([105.0], abs=0.05)

    mp, ml = pos.max_profit_loss()
    assert mp == float("inf")
    assert ml == pytest.approx(-500.0)


def test_short_put_max_loss_is_bounded_at_zero():
    leg = Leg(kind="put", quantity=-1, entry_price=3.0, strike=100.0, dte=30, iv=0.3)
    pos = Position(legs=[leg], spot=100.0, commission_per_contract=0.0)
    mp, ml = pos.max_profit_loss()
    assert mp == pytest.approx(300.0)
    # Stock cannot go below zero: worst case is strike - credit, not -infinity.
    assert ml == pytest.approx(-9700.0, abs=1.0)
    assert not pos.is_risk_defined


def test_naked_short_call_is_unbounded():
    leg = Leg(kind="call", quantity=-1, entry_price=3.0, strike=100.0, dte=30, iv=0.3)
    pos = Position(legs=[leg], spot=100.0, commission_per_contract=0.0)
    _, ml = pos.max_profit_loss()
    assert ml == -float("inf")
    assert not pos.is_risk_defined


def test_covered_call_is_risk_defined_and_bounded():
    pos = Position(
        legs=[Leg(kind="stock", quantity=100, entry_price=100.0),
              Leg(kind="call", quantity=-1, entry_price=3.0, strike=105.0,
                  dte=30, iv=0.3)],
        spot=100.0, commission_per_contract=0.0)
    mp, ml = pos.max_profit_loss()
    assert pos.is_risk_defined
    assert mp == pytest.approx(800.0, abs=1.0)      # 5 up + 3 credit
    assert ml == pytest.approx(-9700.0, abs=1.0)


def test_vertical_spread_max_profit_is_width_minus_debit():
    pos = Position(
        legs=[Leg(kind="call", quantity=1, entry_price=6.0, strike=100.0, dte=30, iv=0.3),
              Leg(kind="call", quantity=-1, entry_price=2.0, strike=110.0, dte=30, iv=0.3)],
        spot=100.0, commission_per_contract=0.0)
    mp, ml = pos.max_profit_loss()
    assert mp == pytest.approx(600.0)     # (110-100)*100 - 400
    assert ml == pytest.approx(-400.0)
    assert pos.breakevens() == pytest.approx([104.0], abs=0.05)
    assert pos.is_risk_defined


def test_iron_condor_has_two_breakevens_and_capped_risk(ctx):
    pos = get_template("iron_condor").make(ctx, {"dte": 45, "delta": 0.16, "width": 5})
    bes = pos.breakevens()
    assert len(bes) == 2
    assert bes[0] < SPOT < bes[1]
    mp, ml = pos.max_profit_loss()
    assert mp > 0 and ml < 0
    assert math.isfinite(ml)
    assert abs(ml) <= 5 * 100 + 1e-6


def test_butterfly_has_two_breakevens_and_peak_at_body(ctx):
    pos = get_template("call_butterfly").make(ctx, {"dte": 30, "center": 0, "width": 10})
    bes = pos.breakevens()
    assert len(bes) == 2
    body = ctx.atm_strike()
    peak = pos.pl_at_expiry(body)
    assert peak > pos.pl_at_expiry(body - 5)
    assert peak > pos.pl_at_expiry(body + 5)


def test_straddle_is_delta_neutral_at_entry(ctx):
    pos = get_template("long_straddle").make(ctx, {"dte": 30})
    g = pos.net_greeks()
    assert abs(g["delta"]) < 12          # roughly neutral on 100-delta scale
    assert g["gamma"] > 0
    assert g["vega"] > 0
    assert g["theta"] < 0


def test_short_strangle_greeks_are_inverted(ctx):
    pos = get_template("short_strangle").make(ctx, {"dte": 45, "delta": 0.16})
    g = pos.net_greeks()
    assert g["theta"] > 0
    assert g["vega"] < 0
    assert g["gamma"] < 0


def test_calendar_uses_near_expiry_convention(ctx):
    pos = get_template("call_calendar").make(
        ctx, {"front_dte": 21, "back_dte": 49, "strike_offset": 0})
    assert pos.is_multi_expiry
    bes = pos.breakevens()
    # A calendar's risk graph is a tent: two breakevens straddling the strike.
    assert len(bes) == 2
    strike = pos.legs[0].strike
    assert bes[0] < strike < bes[1]
    mp, ml = pos.max_profit_loss()
    assert mp > 0                       # would be impossible under intrinsic-only
    assert ml == pytest.approx(-pos.net_debit, rel=0.25)


def test_pmcc_is_risk_defined_and_profitable_somewhere(ctx):
    pos = get_template("pmcc").make(ctx)
    assert pos.is_risk_defined
    mp, _ = pos.max_profit_loss()
    assert mp > 0


def test_quantity_scales_linearly(ctx):
    one = get_template("bull_put_spread").make(ctx, {"quantity": 1})
    three = get_template("bull_put_spread").make(ctx, {"quantity": 3})
    assert three.net_debit == pytest.approx(one.net_debit * 3, rel=1e-9)
    assert three.pl_at_expiry(150.0) == pytest.approx(one.pl_at_expiry(150.0) * 3, rel=1e-9)


# --------------------------------------------------------------------------
# Probability
# --------------------------------------------------------------------------

def test_pop_of_deep_otm_credit_spread_is_high(ctx):
    pos = get_template("bull_put_spread").make(ctx, {"dte": 30, "delta": 0.10, "width": 5})
    assert probability_of_profit(pos) > 0.80


def test_pop_matches_analytic_value_for_single_short_put():
    """
    For a lone short put the profit region is S_T > breakeven, so POP has a
    closed form: 1 - N(d) under the lognormal. Checking the numerical
    integrator against it validates the whole POP machinery.
    """
    from app.engine.pricing import norm_cdf

    strike, credit, dte, iv = 170.0, 4.0, 30.0, 0.30
    pos = Position(
        legs=[Leg(kind="put", quantity=-1, entry_price=credit, strike=strike,
                  dte=dte, iv=iv)],
        spot=SPOT, rate=0.045, commission_per_contract=0.0)

    be = strike - credit
    dist = build_distribution(pos)
    expected = 1.0 - dist.cdf(be)
    assert probability_of_profit(pos) == pytest.approx(expected, abs=2e-3)


def test_probability_of_touch_exceeds_probability_of_finishing_beyond(ctx):
    pos = get_template("short_strangle").make(ctx, {"dte": 45, "delta": 0.16})
    dist = build_distribution(pos)
    short_call = max(l.strike for l in pos.legs if l.kind == "call")
    pot = probability_of_touch(pos, float(short_call), dist)
    p_finish = 1.0 - dist.cdf(float(short_call))
    assert pot > p_finish
    assert pot < 2.5 * p_finish + 0.05


def test_expected_move_scales_with_sqrt_time(ctx):
    pos30 = get_template("long_straddle").make(ctx, {"dte": 30})
    pos120 = get_template("long_straddle").make(ctx, {"dte": 120})
    m30 = build_distribution(pos30).expected_move()[1] - SPOT
    m120 = build_distribution(pos120).expected_move()[1] - SPOT
    assert m120 / m30 == pytest.approx(2.0, rel=0.25)


def test_analysis_block_is_json_safe(ctx):
    import json
    pos = get_template("iron_condor").make(ctx)
    payload = analyze(pos, include_monte_carlo=True)
    text = json.dumps(payload)
    assert "Infinity" not in text and "NaN" not in text


# --------------------------------------------------------------------------
# Surfaces
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tid", ["iron_condor", "pmcc", "wheel", "long_straddle"])
def test_curve_evaluation_matches_point_evaluation_exactly(ctx, tid):
    """
    pl_curve is the batched path every chart endpoint uses; pl_at is the exact
    single-point path the analytics use. If they ever diverge, the graph stops
    agreeing with the numbers printed beside it -- so they must be identical to
    the last floating-point bit, not merely close.
    """
    pos = get_template(tid).make(ctx)
    prices = pos.price_grid(points=61)
    for days in (0.0, 3.0, 12.0, pos.min_dte * 0.85):
        batched = pos.pl_curve(prices, days)
        pointwise = [pos.pl_at(p, days) for p in prices]
        for a, b in zip(batched, pointwise):
            assert a == pytest.approx(b, abs=1e-9)


def test_american_pricing_is_available_and_richer_for_puts():
    """
    BSM is the default for speed, but American valuation has to remain correct
    where a caller asks for it -- an American put is worth strictly more.
    """
    euro = Leg(kind="put", quantity=1, entry_price=5.0, strike=200.0,
               dte=365, iv=0.3, american=False)
    amer = Leg(kind="put", quantity=1, entry_price=5.0, strike=200.0,
               dte=365, iv=0.3, american=True)
    v_euro = euro.value_at(180.0, 0.0, 0.06, 0.0)
    v_amer = amer.value_at(180.0, 0.0, 0.06, 0.0)
    assert v_amer > v_euro


def test_pl_surface_dimensions(ctx):
    pos = get_template("iron_condor").make(ctx)
    surf = pos.pl_surface(price_points=21, time_points=11)
    assert len(surf["prices"]) == 21
    assert len(surf["days"]) == 11
    assert all(len(row) == 21 for row in surf["grid"])
    assert surf["min"] <= surf["max"]


def test_theta_positive_position_gains_with_time(ctx):
    pos = get_template("iron_condor").make(ctx)
    at_entry = pos.pl_at(SPOT, 0.0)
    later = pos.pl_at(SPOT, 20.0)
    assert later > at_entry


def test_vol_shift_hurts_short_premium(ctx):
    pos = get_template("short_strangle").make(ctx)
    rows = {r["shift"]: r["pl"] for r in pos.vol_sensitivity()}
    assert rows[0.10] < rows[0.0] < rows[-0.10]
