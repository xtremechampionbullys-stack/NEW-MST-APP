"""
Probability and risk analytics for a Position.

Probability of profit, expected value, probability of touch, capital
requirement, and a lognormal Monte Carlo for path-dependent questions. These
are the numbers that decide whether a structure is worth putting on -- the
payoff diagram only tells you the shape.

Two probability measures are supported and the distinction is deliberate:

* "risk-neutral" (drift = r - q) is what the option market itself implies.
  It is the honest default: it is internally consistent with the prices you
  are being quoted, and it is what tastytrade-style POP figures approximate.
* "real-world" lets you supply an expected drift. Use it to ask "if I think
  this name compounds at 8%, how does that change the edge?" -- but never
  present it as the market's opinion.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Literal, Sequence

from .payoff import UNBOUNDED, Position
from .pricing import DAYS_PER_YEAR, norm_cdf, norm_ppf, years_from_dte

Measure = Literal["risk-neutral", "real-world"]


# --------------------------------------------------------------------------
# Terminal distribution
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TerminalDist:
    """Lognormal terminal price distribution for the underlying."""
    spot: float
    sigma: float
    T: float
    drift: float          # continuously compounded expected return

    @property
    def mu(self) -> float:
        return math.log(self.spot) + (self.drift - 0.5 * self.sigma ** 2) * self.T

    @property
    def sd(self) -> float:
        return self.sigma * math.sqrt(max(self.T, 1e-9))

    def pdf(self, s: float) -> float:
        if s <= 0.0 or self.sd <= 0.0:
            return 0.0
        z = (math.log(s) - self.mu) / self.sd
        return math.exp(-0.5 * z * z) / (s * self.sd * math.sqrt(2.0 * math.pi))

    def cdf(self, s: float) -> float:
        if s <= 0.0:
            return 0.0
        if self.sd <= 0.0:
            return 1.0 if s >= self.spot else 0.0
        return norm_cdf((math.log(s) - self.mu) / self.sd)

    def quantile(self, p: float) -> float:
        return math.exp(self.mu + self.sd * norm_ppf(p))

    def expected_move(self, sigmas: float = 1.0) -> tuple[float, float]:
        """The +/-1 sigma band traders actually quote."""
        move = self.spot * self.sigma * math.sqrt(max(self.T, 1e-9)) * sigmas
        return self.spot - move, self.spot + move


def build_distribution(position: Position, measure: Measure = "risk-neutral",
                       expected_return: float = 0.0,
                       horizon_days: float | None = None) -> TerminalDist:
    sigma = position.blended_iv or 0.30
    days = horizon_days if horizon_days is not None else (position.min_dte or 30.0)
    T = years_from_dte(days)
    if measure == "risk-neutral":
        drift = position.rate - position.dividend_yield
    else:
        drift = expected_return
    return TerminalDist(spot=position.spot, sigma=sigma, T=max(T, 1e-9), drift=drift)


# --------------------------------------------------------------------------
# Core analytics
# --------------------------------------------------------------------------

def probability_of_profit(position: Position, dist: TerminalDist | None = None,
                          nodes: int = 2000) -> float:
    """
    P(P/L at expiry > 0), integrating the lognormal density over the profitable
    regions. Works for any number of disjoint profit zones, so butterflies and
    ratio spreads come out right where a breakeven-only formula would not.
    """
    d = dist or build_distribution(position)
    lo = max(d.quantile(0.00005), 0.01)
    hi = d.quantile(0.99995)
    step = (hi - lo) / nodes

    total = 0.0
    prev_s = lo
    prev_profit = position.pl_at_expiry(lo) > 0.0
    prev_cdf = d.cdf(lo)

    for i in range(1, nodes + 1):
        s = lo + i * step
        profit = position.pl_at_expiry(s) > 0.0
        if profit != prev_profit:
            # Refine the crossing so narrow zones are not smeared.
            a, b = prev_s, s
            for _ in range(40):
                mid = 0.5 * (a + b)
                if (position.pl_at_expiry(mid) > 0.0) == prev_profit:
                    a = mid
                else:
                    b = mid
            cross = 0.5 * (a + b)
            cross_cdf = d.cdf(cross)
            if prev_profit:
                total += cross_cdf - prev_cdf
            prev_cdf = cross_cdf
            prev_profit = profit
        prev_s = s

    if prev_profit:
        total += d.cdf(hi) - prev_cdf
    return max(0.0, min(1.0, total))


def expected_value(position: Position, dist: TerminalDist | None = None,
                   nodes: int = 1500) -> dict:
    """
    E[P/L] and the tail statistics, by Simpson integration against the terminal
    density. Also returns CVaR at 5% -- the number that tells you what a bad
    week actually costs.
    """
    d = dist or build_distribution(position)
    lo = max(d.quantile(0.00001), 0.01)
    hi = d.quantile(0.99999)
    if nodes % 2:
        nodes += 1
    h = (hi - lo) / nodes

    ev = 0.0
    mass = 0.0
    samples: list[tuple[float, float]] = []
    for i in range(nodes + 1):
        s = lo + i * h
        w = 1 if i in (0, nodes) else (4 if i % 2 else 2)
        dens = d.pdf(s)
        pl = position.pl_at_expiry(s)
        ev += w * pl * dens
        mass += w * dens
        samples.append((dens * h, pl))
    ev *= h / 3.0
    mass *= h / 3.0
    if mass > 1e-9:
        ev /= mass

    samples.sort(key=lambda t: t[1])
    cum = 0.0
    tail_pl = 0.0
    tail_w = 0.0
    for w, pl in samples:
        if cum >= 0.05:
            break
        take = min(w, 0.05 - cum)
        tail_pl += take * pl
        tail_w += take
        cum += take
    cvar5 = tail_pl / tail_w if tail_w > 1e-12 else 0.0

    return {
        "expected_pl": ev,
        "cvar_5pct": cvar5,
        "var_5pct": position.pl_at_expiry(d.quantile(0.05)),
        "var_95pct": position.pl_at_expiry(d.quantile(0.95)),
    }


def probability_of_touch(position: Position, level: float,
                         dist: TerminalDist | None = None) -> float:
    """
    P(the underlying trades through `level` at any point before expiry), under
    GBM with a reflection-principle closed form. Roughly 2x the probability of
    finishing beyond it -- which is why short strikes get tested far more often
    than expiry-only math suggests.
    """
    d = dist or build_distribution(position)
    if level <= 0 or d.sd <= 0:
        return 0.0
    s0 = d.spot
    mu = d.drift - 0.5 * d.sigma ** 2
    a = math.log(level / s0)
    sd = d.sd
    if abs(a) < 1e-12:
        return 1.0

    term1 = norm_cdf((-abs(a) + mu * d.T) / sd) if a > 0 else norm_cdf((-abs(a) - mu * d.T) / sd)
    expo = (2.0 * mu * a) / (d.sigma ** 2)
    expo = max(min(expo, 700.0), -700.0)
    if a > 0:
        term2 = math.exp(expo) * norm_cdf((-a - mu * d.T) / sd)
    else:
        term2 = math.exp(expo) * norm_cdf((a + mu * d.T) / sd)
    return max(0.0, min(1.0, term1 + term2))


def capital_requirement(position: Position) -> dict:
    """
    Approximate Reg-T buying power reduction. This is a planning estimate, not
    a broker calculation -- portfolio-margin accounts and short-stock legs will
    differ, and the real number comes back from the broker adapter at preview
    time.
    """
    _, max_loss = position.max_profit_loss()
    defined = position.is_risk_defined

    if defined and max_loss > -UNBOUNDED:
        req = abs(max_loss)
    else:
        # Naked short approximation: 20% of underlying less OTM amount, per
        # short contract, floored at 10% of strike notional.
        req = 0.0
        for leg in position.legs:
            if leg.kind == "stock" or leg.quantity >= 0:
                continue
            k = float(leg.strike or 0.0)
            otm = max(position.spot - k, 0.0) if leg.kind == "put" else max(k - position.spot, 0.0)
            a = 0.20 * position.spot - otm
            b = 0.10 * (k if leg.kind == "put" else position.spot)
            req += max(a, b) * abs(leg.quantity) * leg.multiplier
        credit = max(position.net_credit, 0.0)
        req = max(req - credit, req * 0.5)

    stock_req = sum(
        abs(leg.quantity) * position.spot * 0.5
        for leg in position.legs if leg.kind == "stock"
    )

    return {
        "estimated_buying_power": req + stock_req,
        "risk_defined": defined,
        "note": "Reg-T estimate. Confirm against your broker's preview endpoint before sizing.",
    }


def monte_carlo(position: Position, paths: int = 2000, steps: int = 24,
                measure: Measure = "risk-neutral", expected_return: float = 0.0,
                seed: int = 7, mark_every: int = 4) -> dict:
    """
    Path simulation for questions the closed forms cannot answer: max adverse
    excursion, probability a short strike is touched intraday, and the
    distribution of P/L if you manage at a profit target.
    """
    rng = random.Random(seed)
    d = build_distribution(position, measure, expected_return)
    dt = d.T / steps
    drift = (d.drift - 0.5 * d.sigma ** 2) * dt
    vol = d.sigma * math.sqrt(dt)

    finals: list[float] = []
    pls: list[float] = []
    mae: list[float] = []
    touched_short = 0

    short_strikes = [float(l.strike) for l in position.legs
                     if l.kind != "stock" and l.quantity < 0 and l.strike]

    for _ in range(paths):
        s = position.spot
        worst = 0.0
        hit = False
        for step_i in range(steps):
            s *= math.exp(drift + vol * rng.gauss(0.0, 1.0))
            # Marking to model on every step is the expensive part; sampling it
            # every few steps gives an MAE estimate at a fraction of the cost.
            if (step_i + 1) % mark_every == 0 or step_i == steps - 1:
                days_elapsed = (step_i + 1) * dt * DAYS_PER_YEAR
                worst = min(worst, position.pl_at(s, min(days_elapsed, position.min_dte)))
            if short_strikes and not hit:
                for k in short_strikes:
                    if (s >= k and position.spot < k) or (s <= k and position.spot > k):
                        hit = True
                        break
        finals.append(s)
        pls.append(position.pl_at_expiry(s))
        mae.append(worst)
        touched_short += 1 if hit else 0

    pls_sorted = sorted(pls)
    n = len(pls_sorted)

    def pct(p: float) -> float:
        return pls_sorted[min(n - 1, max(0, int(p * n)))]

    wins = sum(1 for v in pls if v > 0)
    return {
        "paths": paths,
        "win_rate": wins / n,
        "mean_pl": sum(pls) / n,
        "median_pl": pct(0.5),
        "p05_pl": pct(0.05),
        "p95_pl": pct(0.95),
        "mean_max_adverse": sum(mae) / n,
        "worst_max_adverse": min(mae),
        "prob_short_strike_touched": (touched_short / n) if short_strikes else None,
        "terminal_p05": sorted(finals)[int(0.05 * n)],
        "terminal_p95": sorted(finals)[int(0.95 * n)],
    }


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def analyze(position: Position, measure: Measure = "risk-neutral",
            expected_return: float = 0.0, include_monte_carlo: bool = False) -> dict:
    """One call that produces the full analytics block for the UI."""
    dist = build_distribution(position, measure, expected_return)
    max_profit, max_loss = position.max_profit_loss()
    bes = position.breakevens()
    pop = probability_of_profit(position, dist)
    ev = expected_value(position, dist)
    cap = capital_requirement(position)

    em_lo, em_hi = dist.expected_move(1.0)
    risk = abs(max_loss) if max_loss > -UNBOUNDED else None
    reward = max_profit if max_profit < UNBOUNDED else None

    rr = None
    if risk and reward is not None and risk > 1e-9:
        rr = reward / risk

    pot = {}
    for leg in position.legs:
        if leg.kind != "stock" and leg.quantity < 0 and leg.strike:
            pot[f"{leg.strike:g}{leg.kind[0].upper()}"] = probability_of_touch(
                position, float(leg.strike), dist)

    return {
        "name": position.name,
        "spot": position.spot,
        "net_debit": position.net_debit,
        "net_credit": position.net_credit,
        "is_credit": position.net_credit > 0,
        "commission": position.total_commission,
        "contracts": position.contract_count,
        "max_profit": None if max_profit == UNBOUNDED else max_profit,
        "max_loss": None if max_loss == -UNBOUNDED else max_loss,
        "max_profit_unbounded": max_profit == UNBOUNDED,
        "max_loss_unbounded": max_loss == -UNBOUNDED,
        "risk_defined": position.is_risk_defined,
        "breakevens": bes,
        "probability_of_profit": pop,
        "expected_value": ev["expected_pl"],
        "cvar_5pct": ev["cvar_5pct"],
        "var_5pct": ev["var_5pct"],
        "var_95pct": ev["var_95pct"],
        "risk_reward": rr,
        "return_on_capital": (
            (reward / cap["estimated_buying_power"])
            if reward is not None and cap["estimated_buying_power"] > 1e-9 else None
        ),
        "capital": cap,
        "expected_move_1sd": {"low": em_lo, "high": em_hi},
        "expected_move_pct": (em_hi - position.spot) / position.spot if position.spot else 0.0,
        "blended_iv": position.blended_iv,
        "dte": position.min_dte,
        "max_dte": position.max_dte,
        "multi_expiry": position.is_multi_expiry,
        "probability_of_touch": pot,
        "net_greeks": position.net_greeks(),
        "vol_sensitivity": position.vol_sensitivity(),
        "monte_carlo": monte_carlo(position, measure=measure,
                                   expected_return=expected_return)
        if include_monte_carlo else None,
        "measure": measure,
    }
