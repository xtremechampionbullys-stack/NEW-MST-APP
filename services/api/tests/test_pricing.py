"""
Pricing engine tests.

These check the math against values that are independently verifiable --
textbook Black-Scholes examples, put-call parity, arbitrage bounds, and
convergence of the binomial tree to the analytic price. If the engine is wrong,
every payoff diagram and probability in the product is wrong, so this file is
the foundation everything else stands on.
"""

import math

import pytest

from app.engine.pricing import (
    bjerksund_stensland,
    bs_greeks,
    bs_price,
    crr_price,
    delta_to_strike,
    greeks_for,
    implied_vol,
    norm_cdf,
    norm_ppf,
    price_option,
)


# --------------------------------------------------------------------------
# Distribution helpers
# --------------------------------------------------------------------------

def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-12)
    assert norm_cdf(1.0) == pytest.approx(0.8413447461, abs=1e-9)
    assert norm_cdf(-1.96) == pytest.approx(0.0249978952, abs=1e-9)


def test_norm_ppf_roundtrip():
    for p in (0.001, 0.05, 0.16, 0.5, 0.84, 0.95, 0.999):
        assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-9)


# --------------------------------------------------------------------------
# Black-Scholes
# --------------------------------------------------------------------------

def test_bs_price_textbook_call():
    # Hull, Options Futures and Other Derivatives: S=42, K=40, r=10%, sigma=20%,
    # T=0.5 -> call 4.76, put 0.81
    call = bs_price(42, 40, 0.5, 0.10, 0.0, 0.20, "call")
    put = bs_price(42, 40, 0.5, 0.10, 0.0, 0.20, "put")
    assert call == pytest.approx(4.76, abs=0.01)
    assert put == pytest.approx(0.81, abs=0.01)


def test_put_call_parity():
    S, K, T, r, q, sig = 137.5, 140.0, 0.37, 0.045, 0.012, 0.31
    c = bs_price(S, K, T, r, q, sig, "call")
    p = bs_price(S, K, T, r, q, sig, "put")
    lhs = c - p
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert lhs == pytest.approx(rhs, abs=1e-9)


def test_arbitrage_bounds():
    S, K, T, r, q, sig = 100.0, 95.0, 0.75, 0.05, 0.0, 0.4
    c = bs_price(S, K, T, r, q, sig, "call")
    assert c >= max(S * math.exp(-q * T) - K * math.exp(-r * T), 0.0) - 1e-9
    assert c <= S + 1e-9

    p = bs_price(S, K, T, r, q, sig, "put")
    assert p >= max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0) - 1e-9
    assert p <= K * math.exp(-r * T) + 1e-9


def test_price_is_monotonic_in_vol():
    prev = -1.0
    for sig in (0.05, 0.15, 0.30, 0.60, 1.20):
        px = bs_price(100, 100, 0.5, 0.04, 0.0, sig, "call")
        assert px > prev
        prev = px


def test_expired_option_is_intrinsic():
    assert bs_price(110, 100, 0.0, 0.05, 0.0, 0.3, "call") == pytest.approx(10.0)
    assert bs_price(90, 100, 0.0, 0.05, 0.0, 0.3, "call") == pytest.approx(0.0)
    assert bs_price(90, 100, 0.0, 0.05, 0.0, 0.3, "put") == pytest.approx(10.0)


# --------------------------------------------------------------------------
# Greeks
# --------------------------------------------------------------------------

def test_greeks_against_finite_difference():
    S, K, T, r, q, sig = 185.0, 190.0, 0.25, 0.045, 0.0, 0.33
    g = bs_greeks(S, K, T, r, q, sig, "call")
    h = 1e-4

    fd_delta = (bs_price(S + h, K, T, r, q, sig, "call")
                - bs_price(S - h, K, T, r, q, sig, "call")) / (2 * h)
    assert g.delta == pytest.approx(fd_delta, abs=1e-5)

    fd_gamma = (bs_price(S + h, K, T, r, q, sig, "call")
                - 2 * bs_price(S, K, T, r, q, sig, "call")
                + bs_price(S - h, K, T, r, q, sig, "call")) / (h * h)
    assert g.gamma == pytest.approx(fd_gamma, rel=1e-3)

    dv = 1e-5
    fd_vega = (bs_price(S, K, T, r, q, sig + dv, "call")
               - bs_price(S, K, T, r, q, sig - dv, "call")) / (2 * dv)
    assert g.vega == pytest.approx(fd_vega / 100.0, rel=1e-5)

    dt = 1e-6
    fd_theta = (bs_price(S, K, T - dt, r, q, sig, "call")
                - bs_price(S, K, T + dt, r, q, sig, "call")) / (2 * dt)
    assert g.theta == pytest.approx(fd_theta / 365.0, rel=1e-3)


def test_remaining_greeks_against_finite_difference():
    """The public American path supplies every Greek in its documented units."""
    S, K, T, r, q, sig = 185.0, 190.0, 0.25, 0.045, 0.01, 0.33
    g = greeks_for(S, K, T, r, q, sig, "put", american=True)

    dr = 1e-5
    fd_rho = (price_option(S, K, T, r + dr, q, sig, "put", american=True)
              - price_option(S, K, T, r - dr, q, sig, "put", american=True)) / (2 * dr)
    assert g.rho == pytest.approx(fd_rho / 100.0, rel=5e-2)

    dv = 0.005
    fd_vanna = (greeks_for(S, K, T, r, q, sig + dv, "put", american=True).delta
                - greeks_for(S, K, T, r, q, sig - dv, "put", american=True).delta) / (2 * dv)
    assert g.vanna == pytest.approx(fd_vanna / 100.0, rel=5e-2)

    fd_vomma = (greeks_for(S, K, T, r, q, sig + dv, "put", american=True).vega
                - greeks_for(S, K, T, r, q, sig - dv, "put", american=True).vega) / (2 * dv)
    assert g.vomma == pytest.approx(fd_vomma / 100.0, rel=5e-2)

    dt = 1.0 / 365.0
    fd_charm = (greeks_for(S, K, T - dt, r, q, sig, "put", american=True).delta
                - greeks_for(S, K, T + dt, r, q, sig, "put", american=True).delta) / (2 * dt)
    assert g.charm == pytest.approx(fd_charm / 365.0, rel=5e-2)


def test_delta_bounds_and_parity():
    S, K, T, r, q, sig = 100.0, 100.0, 0.5, 0.04, 0.02, 0.25
    c = bs_greeks(S, K, T, r, q, sig, "call")
    p = bs_greeks(S, K, T, r, q, sig, "put")
    assert 0.0 < c.delta < 1.0
    assert -1.0 < p.delta < 0.0
    # call delta - put delta = e^{-qT}
    assert c.delta - p.delta == pytest.approx(math.exp(-q * T), abs=1e-9)
    # gamma and vega are identical for calls and puts at the same strike
    assert c.gamma == pytest.approx(p.gamma, abs=1e-12)
    assert c.vega == pytest.approx(p.vega, abs=1e-12)


def test_long_option_theta_is_negative():
    for kind in ("call", "put"):
        g = bs_greeks(100, 100, 0.25, 0.03, 0.0, 0.30, kind)
        assert g.theta < 0


# --------------------------------------------------------------------------
# Binomial / American
# --------------------------------------------------------------------------

def test_crr_converges_to_black_scholes_for_european_call():
    S, K, T, r, q, sig = 100.0, 105.0, 0.6, 0.04, 0.0, 0.28
    bs = bs_price(S, K, T, r, q, sig, "call")
    tree = crr_price(S, K, T, r, q, sig, "call", steps=900, american=False)
    assert tree == pytest.approx(bs, abs=0.01)


def test_american_call_no_dividend_equals_european():
    # Classic result: never optimal to exercise an American call early with q=0.
    S, K, T, r, sig = 100.0, 100.0, 1.0, 0.05, 0.30
    eu = bs_price(S, K, T, r, 0.0, sig, "call")
    am = crr_price(S, K, T, r, 0.0, sig, "call", steps=600, american=True)
    assert am == pytest.approx(eu, abs=0.02)


def test_american_put_worth_more_than_european():
    S, K, T, r, sig = 90.0, 100.0, 1.0, 0.08, 0.25
    eu = bs_price(S, K, T, r, 0.0, sig, "put")
    am = crr_price(S, K, T, r, 0.0, sig, "put", steps=600, american=True)
    assert am > eu


def test_american_price_never_below_intrinsic():
    am = crr_price(80.0, 100.0, 0.5, 0.05, 0.0, 0.3, "put", steps=300)
    assert am >= 20.0 - 1e-6


# --------------------------------------------------------------------------
# Bjerksund-Stensland -- the fast American pricer used in production paths
# --------------------------------------------------------------------------

@pytest.mark.parametrize("moneyness", [0.75, 0.9, 1.0, 1.1, 1.3])
@pytest.mark.parametrize("sigma", [0.15, 0.30, 0.60])
@pytest.mark.parametrize("T", [0.08, 0.5, 1.5])
def test_bjerksund_stensland_agrees_with_binomial_put(moneyness, sigma, T):
    """
    The closed form is what actually runs under the risk graph, so it has to
    track the reference tree closely across the range real equity options live
    in. Tolerance is a penny or 3% of premium, whichever is larger -- measured
    against an 800-step tree, the observed worst case across this grid.
    """
    S, r, q = 100.0, 0.05, 0.0
    K = S / moneyness
    approx = bjerksund_stensland(S, K, T, r, q, sigma, "put")
    tree = crr_price(S, K, T, r, q, sigma, "put", steps=800, american=True)
    assert approx == pytest.approx(tree, abs=max(0.01, tree * 0.03))


@pytest.mark.parametrize("q", [0.0, 0.03, 0.08])
def test_bjerksund_stensland_agrees_with_binomial_call(q):
    S, K, T, r, sigma = 100.0, 95.0, 0.75, 0.05, 0.35
    approx = bjerksund_stensland(S, K, T, r, q, sigma, "call")
    tree = crr_price(S, K, T, r, q, sigma, "call", steps=800, american=True)
    assert approx == pytest.approx(tree, abs=max(0.01, tree * 0.03))


def test_bjerksund_stensland_error_is_documented_not_hidden():
    """
    The approximation degrades on deep-ITM, very-long-dated, very-high-vol puts
    -- an extreme corner well outside listed equity options. This test pins the
    known limit so a future regression elsewhere cannot hide behind it.
    """
    approx = bjerksund_stensland(100.0, 166.7, 2.0, 0.05, 0.03, 1.0, "put")
    tree = crr_price(100.0, 166.7, 2.0, 0.05, 0.03, 1.0, "put", steps=800)
    assert approx <= tree                      # the formula is a lower bound
    assert abs(approx - tree) / tree < 0.03


def test_bivariate_normal_identities():
    """The 2002 formula rests on this; a wrong CBND is silently wrong pricing."""
    from app.engine.pricing import bivariate_norm_cdf as M

    # Independence: M(a, b, 0) = N(a) * N(b)
    assert M(0.3, -0.7, 0.0) == pytest.approx(norm_cdf(0.3) * norm_cdf(-0.7), abs=1e-6)
    # Degenerate second argument: M(a, +inf, rho) = N(a)
    assert M(0.42, 8.0, 0.5) == pytest.approx(norm_cdf(0.42), abs=1e-6)
    assert M(-1.1, 8.0, -0.3) == pytest.approx(norm_cdf(-1.1), abs=1e-6)
    # Perfect correlation: M(a, b, 1) = min(N(a), N(b))
    assert M(-1.0, -0.4, 0.999999) == pytest.approx(
        min(norm_cdf(-1.0), norm_cdf(-0.4)), abs=1e-3)
    # Symmetry and bounds
    assert M(0.5, -0.2, 0.4) == pytest.approx(M(-0.2, 0.5, 0.4), abs=1e-9)
    for rho in (-0.9, -0.3, 0.0, 0.3, 0.9):
        for a in (-2.0, -0.5, 0.0, 1.3):
            for b in (-1.7, 0.2, 2.1):
                v = M(a, b, rho)
                assert -1e-9 <= v <= 1.0 + 1e-9
                assert v <= min(norm_cdf(a), norm_cdf(b)) + 1e-6


def test_american_never_below_european_or_intrinsic():
    for kind in ("call", "put"):
        for K in (70.0, 100.0, 140.0):
            am = bjerksund_stensland(100.0, K, 0.6, 0.06, 0.02, 0.3, kind)
            eu = bs_price(100.0, K, 0.6, 0.06, 0.02, 0.3, kind)
            assert am >= eu - 1e-9
            assert am >= max(100.0 - K, 0.0) - 1e-9 if kind == "call" else True


def test_black_scholes_is_fast_enough_for_live_rendering():
    """
    BSM is the default risk-graph pricer precisely because of this budget:
    a payoff curve plus a P/L surface is roughly 20k evaluations, and that has
    to fit comfortably inside one API request.
    """
    import time
    start = time.perf_counter()
    for i in range(20000):
        bs_price(100.0 + (i % 50), 100.0, 0.5, 0.05, 0.0, 0.3, "put")
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"BSM too slow for live rendering: {elapsed:.2f}s"


# --------------------------------------------------------------------------
# Implied volatility
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sigma", [0.08, 0.15, 0.30, 0.65, 1.10, 2.00])
@pytest.mark.parametrize("moneyness", [0.7, 0.9, 1.0, 1.1, 1.4])
def test_implied_vol_roundtrip(sigma, moneyness):
    """
    Round-trip price -> IV -> price, but only where vega is large enough for an
    IV to exist. A deep ITM contract at low vol is all intrinsic: the entire
    0-500% vol range moves its price less than a tick, so there is genuinely no
    IV to recover and the solver is required to say None rather than guess.
    """
    S, T, r, q = 150.0, 0.4, 0.045, 0.01
    K = S * moneyness
    for kind in ("call", "put"):
        px = bs_price(S, K, T, r, q, sigma, kind)
        zero_vol_px = bs_price(S, K, T, r, q, 1e-4, kind)
        iv = implied_vol(px, S, K, T, r, q, kind)

        # If the entire volatility range moves the price by less than float
        # resolution, no IV exists and the solver must say so rather than guess.
        if px - zero_vol_px <= max(abs(px), 1.0) * 1e-12:
            assert iv is None, f"unresolvable IV must return None, got {iv}"
            continue

        assert iv is not None
        assert iv == pytest.approx(sigma, abs=1e-4)


def test_implied_vol_recovers_tiny_but_well_determined_prices():
    """
    A far OTM contract can be worth a fraction of a cent and still have a
    perfectly well-determined implied vol. Bailing out on price magnitude
    (rather than on resolvability) would wrongly discard these.
    """
    S, K, T, r, q, sigma = 150.0, 210.0, 0.4, 0.045, 0.01, 0.08
    px = bs_price(S, K, T, r, q, sigma, "call")
    assert px < 1e-6
    assert implied_vol(px, S, K, T, r, q, "call") == pytest.approx(sigma, abs=1e-4)


def test_implied_vol_returns_none_when_vega_is_negligible():
    # 150 spot, 105 strike, 8% vol -> essentially pure intrinsic.
    px = bs_price(150.0, 105.0, 0.4, 0.045, 0.01, 0.08, "call")
    assert implied_vol(px, 150.0, 105.0, 0.4, 0.045, 0.01, "call") is None


def test_implied_vol_rejects_impossible_prices():
    # Price above the underlying is not achievable for a call.
    assert implied_vol(500.0, 100.0, 100.0, 0.5, 0.04, 0.0, "call") is None
    # Below intrinsic.
    assert implied_vol(0.0, 100.0, 50.0, 0.5, 0.04, 0.0, "call") is None


def test_implied_vol_on_american_put():
    S, K, T, r, q, sigma = 95.0, 100.0, 0.5, 0.06, 0.0, 0.35
    px = crr_price(S, K, T, r, q, sigma, "put")
    iv = implied_vol(px, S, K, T, r, q, "put", american=True)
    assert iv == pytest.approx(sigma, abs=5e-3)


# --------------------------------------------------------------------------
# Delta -> strike inversion
# --------------------------------------------------------------------------

@pytest.mark.parametrize("target", [0.10, 0.16, 0.25, 0.35, 0.50, 0.70, 0.85])
def test_delta_to_strike_inverts(target):
    S, T, r, q, sig = 200.0, 45 / 365, 0.045, 0.0, 0.30
    for kind in ("call", "put"):
        K = delta_to_strike(S, T, r, q, sig, target, kind)
        g = bs_greeks(S, K, T, r, q, sig, kind)
        assert abs(g.delta) == pytest.approx(target, abs=2e-3)


def test_higher_delta_call_has_lower_strike():
    S, T, r, q, sig = 100.0, 0.25, 0.04, 0.0, 0.3
    k10 = delta_to_strike(S, T, r, q, sig, 0.10, "call")
    k50 = delta_to_strike(S, T, r, q, sig, 0.50, "call")
    assert k10 > k50
