"""
Option pricing engine.

Black-Scholes-Merton for European options (with continuous dividend yield),
Cox-Ross-Rubinstein binomial for American exercise, a robust implied-volatility
solver, and the full first/second/third-order greek set.

Everything here is pure functions over floats -- no I/O, no state. The whole
module is deliberately dependency-light (numpy + scipy-free math) so it can be
reused inside backtests, agents, and the Node side via a thin port if needed.

Sign conventions
----------------
* All greeks are returned "per 1 contract of 1 share" (i.e. not multiplied by
  the contract multiplier). Position-level scaling happens in payoff.py.
* theta is per calendar day (annual theta / 365).
* vega is per 1 volatility point (1% = 0.01), i.e. dPrice/dSigma / 100.
* rho is per 1 rate point (1% = 0.01).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

OptionType = Literal["call", "put"]

SQRT_2PI = math.sqrt(2.0 * math.pi)
DAYS_PER_YEAR = 365.0

# Below this many years to expiry we treat the option as expired and fall back
# to intrinsic value. ~8.6 minutes.
MIN_T = 1e-5
MIN_SIGMA = 1e-6


# --------------------------------------------------------------------------
# Normal distribution helpers
# --------------------------------------------------------------------------

def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erf -- accurate to ~1e-15."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """
    Inverse standard normal CDF (Acklam's algorithm, ~1.15e-9 relative error),
    refined with one Halley step against erf.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)

    p_low, p_high = 0.02425, 1.0 - 0.02425

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)

    # One Halley refinement.
    e = 0.5 * math.erfc(-x / math.sqrt(2.0)) - p
    u = e * SQRT_2PI * math.exp(x * x / 2.0)
    x = x - u / (1.0 + x * u / 2.0)
    return x


# --------------------------------------------------------------------------
# Black-Scholes-Merton
# --------------------------------------------------------------------------

def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def intrinsic(S: float, K: float, kind: OptionType) -> float:
    return max(S - K, 0.0) if kind == "call" else max(K - S, 0.0)


def bs_price(S: float, K: float, T: float, r: float, q: float,
             sigma: float, kind: OptionType) -> float:
    """European option value under Black-Scholes-Merton."""
    if S <= 0.0 or K <= 0.0:
        return 0.0
    if T <= MIN_T or sigma <= MIN_SIGMA:
        return intrinsic(S, K, kind)

    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    df_r = math.exp(-r * T)
    df_q = math.exp(-q * T)

    if kind == "call":
        return S * df_q * norm_cdf(d1) - K * df_r * norm_cdf(d2)
    return K * df_r * norm_cdf(-d2) - S * df_q * norm_cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float          # per calendar day
    vega: float           # per 1 vol point (0.01)
    rho: float            # per 1 rate point (0.01)
    vanna: float = 0.0    # d delta / d sigma  (per vol point)
    charm: float = 0.0    # d delta / d time   (per calendar day)
    vomma: float = 0.0    # d vega  / d sigma  (per vol point)
    d1: float = 0.0
    d2: float = 0.0

    def as_dict(self) -> dict:
        return {
            "price": self.price, "delta": self.delta, "gamma": self.gamma,
            "theta": self.theta, "vega": self.vega, "rho": self.rho,
            "vanna": self.vanna, "charm": self.charm, "vomma": self.vomma,
        }


def bs_greeks(S: float, K: float, T: float, r: float, q: float,
              sigma: float, kind: OptionType) -> Greeks:
    """Full analytic greek set for a European option."""
    if S <= 0.0 or K <= 0.0 or T <= MIN_T or sigma <= MIN_SIGMA:
        px = intrinsic(S, K, kind)
        if kind == "call":
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return Greeks(price=px, delta=delta, gamma=0.0, theta=0.0,
                      vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    sqrt_t = math.sqrt(T)
    df_r = math.exp(-r * T)
    df_q = math.exp(-q * T)
    pdf_d1 = norm_pdf(d1)

    price = bs_price(S, K, T, r, q, sigma, kind)
    gamma = df_q * pdf_d1 / (S * sigma * sqrt_t)
    vega_annual = S * df_q * pdf_d1 * sqrt_t
    vanna_annual = -df_q * pdf_d1 * d2 / sigma
    vomma_annual = vega_annual * d1 * d2 / sigma

    if kind == "call":
        delta = df_q * norm_cdf(d1)
        theta_annual = (
            -(S * df_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
            - r * K * df_r * norm_cdf(d2)
            + q * S * df_q * norm_cdf(d1)
        )
        rho_annual = K * T * df_r * norm_cdf(d2)
        charm_annual = (
            -df_q * (pdf_d1 * (2.0 * (r - q) * T - d2 * sigma * sqrt_t)
                     / (2.0 * T * sigma * sqrt_t))
            + q * df_q * norm_cdf(d1)
        )
    else:
        delta = -df_q * norm_cdf(-d1)
        theta_annual = (
            -(S * df_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
            + r * K * df_r * norm_cdf(-d2)
            - q * S * df_q * norm_cdf(-d1)
        )
        rho_annual = -K * T * df_r * norm_cdf(-d2)
        charm_annual = (
            -df_q * (pdf_d1 * (2.0 * (r - q) * T - d2 * sigma * sqrt_t)
                     / (2.0 * T * sigma * sqrt_t))
            - q * df_q * norm_cdf(-d1)
        )

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        theta=theta_annual / DAYS_PER_YEAR,
        vega=vega_annual / 100.0,
        rho=rho_annual / 100.0,
        vanna=vanna_annual / 100.0,
        charm=charm_annual / DAYS_PER_YEAR,
        vomma=vomma_annual / 10000.0,
        d1=d1,
        d2=d2,
    )


# --------------------------------------------------------------------------
# American exercise -- Cox-Ross-Rubinstein binomial
# --------------------------------------------------------------------------

def crr_price(S: float, K: float, T: float, r: float, q: float, sigma: float,
              kind: OptionType, steps: int = 160, american: bool = True) -> float:
    """
    Cox-Ross-Rubinstein binomial tree. Used for American puts and for any
    option on a dividend-paying underlying where early exercise matters.
    """
    if T <= MIN_T or sigma <= MIN_SIGMA:
        return intrinsic(S, K, kind)

    steps = max(8, int(steps))
    dt = T / steps
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-r * dt)
    p = (math.exp((r - q) * dt) - d) / (u - d)
    p = min(max(p, 0.0), 1.0)

    # Terminal payoffs.
    values = []
    for i in range(steps + 1):
        ST = S * (u ** (steps - i)) * (d ** i)
        values.append(intrinsic(ST, K, kind))

    for step in range(steps - 1, -1, -1):
        for i in range(step + 1):
            cont = disc * (p * values[i] + (1.0 - p) * values[i + 1])
            if american:
                ST = S * (u ** (step - i)) * (d ** i)
                cont = max(cont, intrinsic(ST, K, kind))
            values[i] = cont

    return values[0]


# --------------------------------------------------------------------------
# American exercise -- Bjerksund-Stensland closed form
# --------------------------------------------------------------------------
#
# The binomial tree above is correct but far too slow to sit underneath a live
# risk graph: one payoff curve is thousands of evaluations, and the P/L surface
# is tens of thousands more. Bjerksund-Stensland (1993) gives a closed-form
# approximation to the American price in O(1), typically within a fraction of a
# cent of the tree. The tree stays as the reference implementation and the test
# suite asserts the two agree, so we get speed without giving up correctness.

_DW_W = (0.24840615, 0.39233107, 0.21141819, 0.03324666, 0.00082485334)
_DW_X = (0.10024215, 0.48281397, 1.06094972, 1.77972941, 2.66976035001)


def bivariate_norm_cdf(a: float, b: float, rho: float) -> float:
    """
    Standard bivariate normal CDF, Drezner-Wesolowsky 5-point quadrature with
    the usual quadrant reductions. Accurate to about 1e-6, which is far tighter
    than anything downstream needs. Required by the two-step Bjerksund-Stensland
    formula below.
    """
    rho = max(min(rho, 1.0 - 1e-12), -1.0 + 1e-12)

    if a <= 0.0 and b <= 0.0 and rho <= 0.0:
        denom = math.sqrt(2.0 * (1.0 - rho * rho))
        aa, bb = a / denom, b / denom
        total = 0.0
        for i in range(5):
            for j in range(5):
                total += _DW_W[i] * _DW_W[j] * math.exp(
                    aa * (2.0 * _DW_X[i] - aa)
                    + bb * (2.0 * _DW_X[j] - bb)
                    + 2.0 * rho * (_DW_X[i] - aa) * (_DW_X[j] - bb)
                )
        return math.sqrt(1.0 - rho * rho) / math.pi * total

    if a <= 0.0 <= b and rho >= 0.0:
        return norm_cdf(a) - bivariate_norm_cdf(a, -b, -rho)
    if b <= 0.0 <= a and rho >= 0.0:
        return norm_cdf(b) - bivariate_norm_cdf(-a, b, -rho)
    if a >= 0.0 and b >= 0.0 and rho <= 0.0:
        return norm_cdf(a) + norm_cdf(b) - 1.0 + bivariate_norm_cdf(-a, -b, rho)

    # Remaining case: a*b*rho > 0.
    denom = math.sqrt(a * a - 2.0 * rho * a * b + b * b)
    if denom < 1e-14:
        return max(0.0, min(1.0, norm_cdf(a) + norm_cdf(b) - 1.0))
    sgn_a = 1.0 if a >= 0.0 else -1.0
    sgn_b = 1.0 if b >= 0.0 else -1.0
    rho1 = (rho * a - b) * sgn_a / denom
    rho2 = (rho * b - a) * sgn_b / denom
    delta = (1.0 - sgn_a * sgn_b) / 4.0
    return (bivariate_norm_cdf(a, 0.0, rho1)
            + bivariate_norm_cdf(b, 0.0, rho2) - delta)


def _bs_phi(S: float, T: float, gamma: float, H: float, I: float,
            r: float, b: float, sigma: float) -> float:
    v2 = sigma * sigma
    lam = (-r + gamma * b + 0.5 * gamma * (gamma - 1.0) * v2) * T
    kappa = (2.0 * b / v2) + (2.0 * gamma - 1.0)
    sqt = sigma * math.sqrt(T)
    d = -(math.log(S / H) + (b + (gamma - 0.5) * v2) * T) / sqt
    d2 = d - 2.0 * math.log(I / S) / sqt
    lam = max(min(lam, 700.0), -700.0)
    return (math.exp(lam) * (S ** gamma)
            * (norm_cdf(d) - ((I / S) ** kappa) * norm_cdf(d2)))


def _bs_psi(S: float, T2: float, gamma: float, H: float, I2: float, I1: float,
            t1: float, r: float, b: float, sigma: float) -> float:
    """The two-boundary term of Bjerksund-Stensland (2002)."""
    v2 = sigma * sigma
    drift = b + (gamma - 0.5) * v2
    s1 = sigma * math.sqrt(t1)
    s2 = sigma * math.sqrt(T2)

    e1 = (math.log(S / I1) + drift * t1) / s1
    e2 = (math.log(I2 * I2 / (S * I1)) + drift * t1) / s1
    e3 = (math.log(S / I1) - drift * t1) / s1
    e4 = (math.log(I2 * I2 / (S * I1)) - drift * t1) / s1

    f1 = (math.log(S / H) + drift * T2) / s2
    f2 = (math.log(I2 * I2 / (S * H)) + drift * T2) / s2
    f3 = (math.log(I1 * I1 / (S * H)) + drift * T2) / s2
    f4 = (math.log(S * I1 * I1 / (H * I2 * I2)) + drift * T2) / s2

    rho = math.sqrt(t1 / T2)
    lam = (-r + gamma * b + 0.5 * gamma * (gamma - 1.0) * v2) * T2
    lam = max(min(lam, 700.0), -700.0)
    kappa = (2.0 * b / v2) + (2.0 * gamma - 1.0)

    return math.exp(lam) * (S ** gamma) * (
        bivariate_norm_cdf(-e1, -f1, rho)
        - ((I2 / S) ** kappa) * bivariate_norm_cdf(-e2, -f2, rho)
        - ((I1 / S) ** kappa) * bivariate_norm_cdf(-e3, -f3, -rho)
        + ((I1 / I2) ** kappa) * bivariate_norm_cdf(-e4, -f4, -rho)
    )


def _bs_american_call(S: float, K: float, T: float, r: float, b: float,
                      sigma: float) -> float:
    """
    American call with cost of carry b (= r - q), Bjerksund-Stensland (2002).

    The 2002 form splits the life of the option at t1 and uses two exercise
    boundaries instead of one, which cuts the error of the 1993 single-boundary
    version by roughly an order of magnitude. When b >= r the carry never makes
    early exercise optimal, so the European value is exact.
    """
    if b >= r:
        q = r - b
        return bs_price(S, K, T, r, q, sigma, "call")

    v2 = sigma * sigma
    beta = (0.5 - b / v2) + math.sqrt((b / v2 - 0.5) ** 2 + 2.0 * r / v2)
    if abs(beta - 1.0) < 1e-9:
        beta = 1.0 + 1e-9

    b_inf = (beta / (beta - 1.0)) * K
    b_zero = max(K, (r / (r - b)) * K) if abs(r - b) > 1e-12 else K
    if b_inf <= b_zero:
        b_inf = b_zero * (1.0 + 1e-7)

    t1 = 0.5 * (math.sqrt(5.0) - 1.0) * T
    spread = (b_inf - b_zero) * b_zero
    if spread < 1e-12:
        spread = 1e-12

    h1 = -(b * t1 + 2.0 * sigma * math.sqrt(t1)) * (K * K) / spread
    h2 = -(b * T + 2.0 * sigma * math.sqrt(T)) * (K * K) / spread
    h1 = max(min(h1, 700.0), -700.0)
    h2 = max(min(h2, 700.0), -700.0)

    i1 = b_zero + (b_inf - b_zero) * (1.0 - math.exp(h1))
    i2 = b_zero + (b_inf - b_zero) * (1.0 - math.exp(h2))
    i1 = max(i1, K * (1.0 + 1e-9))
    i2 = max(i2, i1 * (1.0 + 1e-9))

    if S >= i2:
        return S - K

    alpha1 = (i1 - K) * (i1 ** (-beta))
    alpha2 = (i2 - K) * (i2 ** (-beta))

    return (
        alpha2 * (S ** beta)
        - alpha2 * _bs_phi(S, t1, beta, i2, i2, r, b, sigma)
        + _bs_phi(S, t1, 1.0, i2, i2, r, b, sigma)
        - _bs_phi(S, t1, 1.0, i1, i2, r, b, sigma)
        - K * _bs_phi(S, t1, 0.0, i2, i2, r, b, sigma)
        + K * _bs_phi(S, t1, 0.0, i1, i2, r, b, sigma)
        + alpha1 * _bs_phi(S, t1, beta, i1, i2, r, b, sigma)
        - alpha1 * _bs_psi(S, T, beta, i1, i2, i1, t1, r, b, sigma)
        + _bs_psi(S, T, 1.0, i1, i2, i1, t1, r, b, sigma)
        - _bs_psi(S, T, 1.0, K, i2, i1, t1, r, b, sigma)
        - K * _bs_psi(S, T, 0.0, i1, i2, i1, t1, r, b, sigma)
        + K * _bs_psi(S, T, 0.0, K, i2, i1, t1, r, b, sigma)
    )


def bjerksund_stensland(S: float, K: float, T: float, r: float, q: float,
                        sigma: float, kind: OptionType) -> float:
    """
    American option value. Puts use the standard transformation
    P(S, K, T, r, b) = C(K, S, T, r - b, -b), so one call implementation
    serves both.
    """
    if T <= MIN_T or sigma <= MIN_SIGMA or S <= 0.0 or K <= 0.0:
        return intrinsic(S, K, kind)

    b = r - q
    if kind == "call":
        val = _bs_american_call(S, K, T, r, b, sigma)
    else:
        val = _bs_american_call(K, S, T, r - b, -b, sigma)

    # The approximation can dip a hair below intrinsic or below the European
    # value near the exercise boundary; both are arbitrage violations.
    european = bs_price(S, K, T, r, q, sigma, kind)
    return max(val, european, intrinsic(S, K, kind))


def american_greeks(S: float, K: float, T: float, r: float, q: float,
                    sigma: float, kind: OptionType, steps: int = 160) -> Greeks:
    """Central-difference greeks off the closed-form American price."""
    f = bjerksund_stensland
    h = max(S * 0.002, 0.005)
    p0 = f(S, K, T, r, q, sigma, kind)
    pu = f(S + h, K, T, r, q, sigma, kind)
    pd = f(S - h, K, T, r, q, sigma, kind)

    delta = (pu - pd) / (2.0 * h)
    gamma = (pu - 2.0 * p0 + pd) / (h * h)

    dv = 0.005
    vega = (f(S, K, T, r, q, sigma + dv, kind)
            - f(S, K, T, r, q, max(sigma - dv, 1e-4), kind)) / (2.0 * dv) / 100.0

    def delta_at(volatility: float, time: float = T) -> float:
        return (f(S + h, K, time, r, q, volatility, kind)
                - f(S - h, K, time, r, q, volatility, kind)) / (2.0 * h)

    def vega_at(volatility: float) -> float:
        return (f(S, K, T, r, q, volatility + dv, kind)
                - f(S, K, T, r, q, max(volatility - dv, 1e-4), kind)) / (2.0 * dv) / 100.0

    vanna = (delta_at(sigma + dv) - delta_at(max(sigma - dv, 1e-4))) / (2.0 * dv) / 100.0
    vomma = (vega_at(sigma + dv) - vega_at(max(sigma - dv, 1e-4))) / (2.0 * dv) / 100.0

    dt = min(1.0 / DAYS_PER_YEAR, T * 0.5)
    theta = (f(S, K, max(T - dt, MIN_T), r, q, sigma, kind) - p0) / (dt * DAYS_PER_YEAR)
    charm = (delta_at(sigma, max(T - dt, MIN_T)) - delta_at(sigma, T + dt)) / (2.0 * dt * DAYS_PER_YEAR)

    dr = 0.0005
    rho = (f(S, K, T, r + dr, q, sigma, kind)
           - f(S, K, T, r - dr, q, sigma, kind)) / (2.0 * dr) / 100.0

    return Greeks(price=p0, delta=delta, gamma=gamma, theta=theta,
                  vega=vega, rho=rho, vanna=vanna, charm=charm, vomma=vomma)


# --------------------------------------------------------------------------
# Implied volatility
# --------------------------------------------------------------------------

def implied_vol(price: float, S: float, K: float, T: float, r: float, q: float,
                kind: OptionType, *, american: bool = False,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-7,
                max_iter: int = 100) -> float | None:
    """
    Solve for sigma given a market price.

    Newton-Raphson seeded with the Brenner-Subrahmanyam approximation, with a
    bisection fallback so we always converge or honestly return None. Returns
    None when the price is outside the no-arbitrage bounds (a stale or crossed
    quote), which is common enough in real chains that silently returning 0
    would poison downstream analytics.
    """
    if T <= MIN_T or price <= 0.0 or S <= 0.0 or K <= 0.0:
        return None

    pricer = (lambda s: bjerksund_stensland(S, K, T, r, q, s, kind)) if american else \
             (lambda s: bs_price(S, K, T, r, q, s, kind))

    lo_price, hi_price = pricer(lo), pricer(hi)

    # A deep in-the-money contract is almost pure intrinsic: the whole 0-500%
    # vol range moves its price by less than one part in 1e12, so the volatility
    # information is destroyed by floating-point resolution and there is no IV
    # to recover. Returning the floor there would be a lie that poisons the
    # surface -- better to say so and let the caller interpolate from
    # neighbouring strikes. Note this is a test on *resolvability*, not on price
    # magnitude: a far OTM contract worth 1e-10 still has a perfectly
    # well-determined IV, because its price is small but not imprecise.
    resolution = max(abs(price), 1.0) * 1e-12
    if price - lo_price <= resolution:
        return None
    if price >= hi_price:
        return None

    # Bisection, bracketed by the bounds checked above so convergence is
    # guaranteed. We deliberately converge on the *volatility* interval rather
    # than on the price residual: a far OTM contract is worth fractions of a
    # cent, so every candidate sigma satisfies an absolute price tolerance and a
    # residual-based stop would return whatever it happened to try first. Newton
    # would be a handful of iterations faster, but ~26 bisection steps is about
    # 15 microseconds -- irrelevant next to being right on every contract in the
    # chain, including the illiquid wings where quotes are worst.
    a, b = lo, hi
    for _ in range(max_iter):
        mid = 0.5 * (a + b)
        if (b - a) < tol:
            return mid
        if pricer(mid) < price:
            a = mid
        else:
            b = mid
    return 0.5 * (a + b)


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------

def price_option(S: float, K: float, T: float, r: float, q: float, sigma: float,
                 kind: OptionType, *, american: bool = False) -> float:
    """
    Single entry point used by the whole system. American pricing only kicks in
    where early exercise can actually have value -- puts, or calls on a
    dividend-paying underlying -- so the common case stays a plain BSM call.
    """
    if american and (kind == "put" or q > 0.0):
        return bjerksund_stensland(S, K, T, r, q, sigma, kind)
    return bs_price(S, K, T, r, q, sigma, kind)


def greeks_for(S: float, K: float, T: float, r: float, q: float, sigma: float,
               kind: OptionType, *, american: bool = False) -> Greeks:
    if american and (kind == "put" or q > 0.0):
        return american_greeks(S, K, T, r, q, sigma, kind)
    return bs_greeks(S, K, T, r, q, sigma, kind)


def years_from_dte(dte: float) -> float:
    """Calendar-day DTE -> year fraction, floored so 0DTE stays finite."""
    return max(float(dte), 0.0) / DAYS_PER_YEAR


def delta_to_strike(S: float, T: float, r: float, q: float, sigma: float,
                    target_delta: float, kind: OptionType) -> float:
    """
    Invert delta to a strike. Used by strategy templates that are specified in
    delta terms ("sell the 16-delta strangle") rather than absolute strikes.
    """
    target = abs(target_delta)
    target = min(max(target, 1e-4), 1.0 - 1e-4)
    df_q = math.exp(-q * T)
    n_d1 = target / df_q
    n_d1 = min(max(n_d1, 1e-6), 1.0 - 1e-6)
    d1 = norm_ppf(n_d1) if kind == "call" else -norm_ppf(n_d1)
    v = sigma * math.sqrt(T)
    return S * math.exp(-(d1 * v) + (r - q + 0.5 * sigma * sigma) * T)
