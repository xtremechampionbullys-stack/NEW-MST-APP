"""
Multi-leg position math.

Takes a set of legs (options and/or stock) and produces everything the
strategy tester renders: expiration payoff, T+N theoretical curves, breakevens,
max profit/loss, net greeks, and the price x time P/L surface.

The core abstraction is `Position` -- a bag of legs plus a market snapshot
(spot, rate, dividend yield). Every strategy template in strategies.py builds
one of these, so condors and wheels and PMCCs all flow through identical math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Sequence

from .pricing import (
    DAYS_PER_YEAR,
    Greeks,
    bjerksund_stensland,
    bs_price,
    greeks_for,
    intrinsic,
    price_option,
    years_from_dte,
)

LegKind = Literal["call", "put", "stock"]
UNBOUNDED = float("inf")


# --------------------------------------------------------------------------
# Legs
# --------------------------------------------------------------------------

@dataclass
class Leg:
    """
    One component of a strategy.

    quantity is signed: +2 = long 2 contracts, -1 = short 1 contract.
    For stock legs, `multiplier` is 1 and quantity is in shares.
    """
    kind: LegKind
    quantity: float
    entry_price: float                 # per share, debit positive
    strike: float | None = None
    dte: float = 0.0                   # calendar days to expiry at entry
    iv: float = 0.30
    multiplier: float = 100.0
    # European (BSM) by default -- see the note above _leg_curve. Set True for
    # exact American valuation on a leg where early exercise matters.
    american: bool = False
    label: str | None = None
    symbol: str | None = None

    def __post_init__(self):
        if self.kind == "stock":
            self.strike = None
            self.dte = 0.0
            self.multiplier = 1.0
        elif self.strike is None:
            raise ValueError("option legs require a strike")

    @property
    def is_short(self) -> bool:
        return self.quantity < 0

    @property
    def notional_qty(self) -> float:
        return self.quantity * self.multiplier

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.kind == "stock":
            side = "Long" if self.quantity > 0 else "Short"
            return f"{side} {abs(self.quantity):g} shares"
        side = "Long" if self.quantity > 0 else "Short"
        return (f"{side} {abs(self.quantity):g}x {self.strike:g} "
                f"{self.kind.capitalize()} {self.dte:g}DTE")

    # ---- valuation -------------------------------------------------------

    def value_at_expiry(self, spot: float) -> float:
        """Per-share intrinsic value at this leg's expiry."""
        if self.kind == "stock":
            return spot
        return intrinsic(spot, float(self.strike), self.kind)

    def value_at(self, spot: float, days_elapsed: float, r: float, q: float,
                 iv_shift: float = 0.0) -> float:
        """Per-share theoretical value `days_elapsed` days after entry."""
        if self.kind == "stock":
            return spot
        remaining = self.dte - days_elapsed
        if remaining <= 0.0:
            return intrinsic(spot, float(self.strike), self.kind)
        sigma = max(self.iv + iv_shift, 1e-4)
        return price_option(
            spot, float(self.strike), years_from_dte(remaining),
            r, q, sigma, self.kind, american=self.american,
        )

    def greeks_at(self, spot: float, days_elapsed: float, r: float, q: float,
                  iv_shift: float = 0.0) -> Greeks:
        if self.kind == "stock":
            return Greeks(price=spot, delta=1.0, gamma=0.0, theta=0.0,
                          vega=0.0, rho=0.0)
        remaining = max(self.dte - days_elapsed, 0.0)
        sigma = max(self.iv + iv_shift, 1e-4)
        return greeks_for(
            spot, float(self.strike), years_from_dte(remaining),
            r, q, sigma, self.kind, american=self.american,
        )


# --------------------------------------------------------------------------
# Position
# --------------------------------------------------------------------------

@dataclass
class Position:
    legs: list[Leg]
    spot: float
    rate: float = 0.045
    dividend_yield: float = 0.0
    name: str = "Custom"
    commission_per_contract: float = 0.65
    metadata: dict = field(default_factory=dict)

    # ---- cost basis ------------------------------------------------------

    @property
    def net_debit(self) -> float:
        """Positive = you pay. Negative = you collect. Includes commissions."""
        cost = sum(leg.quantity * leg.entry_price * leg.multiplier for leg in self.legs)
        return cost + self.total_commission

    @property
    def total_commission(self) -> float:
        return sum(
            abs(leg.quantity) * self.commission_per_contract
            for leg in self.legs if leg.kind != "stock"
        )

    @property
    def net_credit(self) -> float:
        return -self.net_debit

    @property
    def contract_count(self) -> int:
        return int(sum(abs(leg.quantity) for leg in self.legs if leg.kind != "stock"))

    @property
    def max_dte(self) -> float:
        opts = [leg.dte for leg in self.legs if leg.kind != "stock"]
        return max(opts) if opts else 0.0

    @property
    def min_dte(self) -> float:
        opts = [leg.dte for leg in self.legs if leg.kind != "stock"]
        return min(opts) if opts else 0.0

    @property
    def is_multi_expiry(self) -> bool:
        opts = {leg.dte for leg in self.legs if leg.kind != "stock"}
        return len(opts) > 1

    @property
    def blended_iv(self) -> float:
        opts = [leg for leg in self.legs if leg.kind != "stock"]
        if not opts:
            return 0.0
        w = sum(abs(leg.quantity) for leg in opts) or 1.0
        return sum(leg.iv * abs(leg.quantity) for leg in opts) / w

    # ---- P/L -------------------------------------------------------------

    def pl_at_expiry(self, spot: float) -> float:
        """
        The risk-graph "expiration" line.

        For a single-expiry position this is plain intrinsic value. For a
        multi-expiry position (calendars, diagonals, PMCC) valuing every leg at
        intrinsic would be nonsense -- the back leg still has months of life --
        so we evaluate at the *nearest* expiry with the longer-dated legs marked
        to model. That matches how every risk-graph tool draws it, and it is the
        only view that makes POP and breakevens meaningful for these structures.
        """
        if self.is_multi_expiry:
            return self.pl_at(spot, self.min_dte)
        gross = sum(
            leg.quantity * leg.value_at_expiry(spot) * leg.multiplier
            for leg in self.legs
        )
        return gross - self.net_debit

    def pl_at(self, spot: float, days_elapsed: float = 0.0,
              iv_shift: float = 0.0) -> float:
        """Mark-to-model dollar P/L at a given spot / time / vol shift."""
        gross = sum(
            leg.quantity * leg.value_at(spot, days_elapsed, self.rate,
                                        self.dividend_yield, iv_shift)
            * leg.multiplier
            for leg in self.legs
        )
        return gross - self.net_debit

    # ---- curve evaluation ------------------------------------------------
    #
    # A note on the pricing model, because it is a real trade-off and hiding it
    # would be worse than explaining it.
    #
    # Black-Scholes evaluates in ~0.5us; the closed-form American price is
    # ~300us. A payoff curve is hundreds of points and a P/L surface thousands,
    # times every leg -- so American curve rendering costs on the order of a
    # second per request, while BSM is a couple of milliseconds.
    #
    # We default legs to European (BSM) valuation for the risk graph. For the
    # region of the curve anyone actually trades -- at and near the money, 30-60
    # DTE -- the early-exercise premium on a US equity put is well under a cent
    # per share, so the two models are visually and numerically identical there.
    # The premium only becomes material deep in the money, where it is also
    # least decision-relevant. American pricing stays available per-leg
    # (Leg.american) and is used for point valuations where it is cheap.
    #
    # What we do NOT do is interpolate the American price to fake speed: the
    # early-exercise premium develops a kink at the exercise boundary as expiry
    # approaches, and interpolating through it produced dollars of silent error
    # in testing. An honest fast model beats a dishonest accurate-looking one.

    def _leg_curve(self, leg: Leg, prices: Sequence[float], days_elapsed: float,
                   iv_shift: float) -> list[float]:
        if leg.kind == "stock":
            return list(prices)

        remaining = leg.dte - days_elapsed
        if remaining <= 0.0:
            return [leg.value_at_expiry(p) for p in prices]

        sigma = max(leg.iv + iv_shift, 1e-4)
        T = years_from_dte(remaining)
        K = float(leg.strike)
        r, q = self.rate, self.dividend_yield

        if leg.american and (leg.kind == "put" or q > 0.0):
            return [bjerksund_stensland(p, K, T, r, q, sigma, leg.kind)
                    for p in prices]
        return [bs_price(p, K, T, r, q, sigma, leg.kind) for p in prices]

    def pl_curve(self, prices: Sequence[float], days_elapsed: float = 0.0,
                 iv_shift: float = 0.0) -> list[float]:
        """
        pl_at evaluated across a price array. Identical results to calling
        pl_at point by point -- it just hoists the per-leg setup out of the
        inner loop. Every chart endpoint goes through here.
        """
        totals = [0.0] * len(prices)
        for leg in self.legs:
            vals = self._leg_curve(leg, prices, days_elapsed, iv_shift)
            scale = leg.quantity * leg.multiplier
            for i, v in enumerate(vals):
                totals[i] += v * scale
        debit = self.net_debit
        return [t - debit for t in totals]

    def expiry_pl_curve(self, prices: Sequence[float]) -> list[float]:
        if self.is_multi_expiry:
            return self.pl_curve(prices, self.min_dte)
        out = []
        debit = self.net_debit
        for p in prices:
            gross = sum(leg.quantity * leg.value_at_expiry(p) * leg.multiplier
                        for leg in self.legs)
            out.append(gross - debit)
        return out

    # ---- curves ----------------------------------------------------------

    def price_grid(self, points: int = 161, width_sigma: float = 3.0) -> list[float]:
        """
        A price axis wide enough to show all the structure: +/- N sigma over the
        longest expiry, expanded to comfortably contain every strike.
        """
        iv = self.blended_iv or 0.30
        t = years_from_dte(self.max_dte or 30.0)
        move = self.spot * iv * math.sqrt(max(t, 1.0 / DAYS_PER_YEAR)) * width_sigma
        lo = self.spot - move
        hi = self.spot + move

        strikes = [leg.strike for leg in self.legs if leg.strike]
        if strikes:
            pad = (max(strikes) - min(strikes)) * 0.35 + self.spot * 0.05
            lo = min(lo, min(strikes) - pad)
            hi = max(hi, max(strikes) + pad)

        lo = max(lo, 0.01)
        hi = max(hi, lo * 1.02)
        step = (hi - lo) / (points - 1)
        return [lo + i * step for i in range(points)]

    def expiry_curve(self, prices: Sequence[float] | None = None) -> dict:
        px = list(prices) if prices is not None else self.price_grid()
        return {"prices": px, "pl": self.expiry_pl_curve(px)}

    def theoretical_curves(self, day_offsets: Sequence[float] | None = None,
                           prices: Sequence[float] | None = None,
                           iv_shift: float = 0.0) -> list[dict]:
        """
        T+0 / T+N curves. Defaults to a sensible spread: today, a third of the
        way to expiry, two thirds, and expiry itself.
        """
        px = list(prices) if prices is not None else self.price_grid()
        if day_offsets is None:
            dte = self.min_dte or 30.0
            day_offsets = [0.0, round(dte / 3.0, 2), round(2.0 * dte / 3.0, 2), dte]

        out = []
        for d in day_offsets:
            out.append({
                "days": d,
                "label": "Expiry" if d >= (self.min_dte or 0) else f"T+{d:g}",
                "pl": self.pl_curve(px, d, iv_shift),
            })
        return out

    # ---- key levels ------------------------------------------------------

    def breakevens(self, resolution: int = 4000) -> list[float]:
        """
        Sign changes of the expiry P/L curve, refined by bisection. Handles the
        many-breakeven cases (condors, butterflies, ratio spreads) that a
        closed-form solver would miss.
        """
        px = self.price_grid(points=resolution, width_sigma=5.0)
        vals = self.expiry_pl_curve(px)
        roots: list[float] = []

        for i in range(len(px) - 1):
            a, b = vals[i], vals[i + 1]
            if a == 0.0:
                roots.append(px[i])
                continue
            if a * b < 0.0:
                lo, hi = px[i], px[i + 1]
                for _ in range(60):
                    mid = 0.5 * (lo + hi)
                    vm = self.pl_at_expiry(mid)
                    if vm == 0.0:
                        break
                    if (self.pl_at_expiry(lo) < 0) != (vm < 0):
                        hi = mid
                    else:
                        lo = mid
                roots.append(round(0.5 * (lo + hi), 4))

        deduped: list[float] = []
        for rt in sorted(roots):
            if not deduped or abs(rt - deduped[-1]) > max(0.01, self.spot * 1e-4):
                deduped.append(rt)
        return deduped

    def max_profit_loss(self) -> tuple[float, float]:
        """
        (max_profit, max_loss).

        Only the upside can be genuinely unbounded -- a share price cannot go
        below zero, so downside is always a finite (if large) number, and
        reporting "unlimited loss" for a cash-secured put would be wrong. We
        therefore scan the expiry curve, anchor the downside at spot -> 0, and
        only return infinity when the far-upside slope says a short call is
        uncovered.
        """
        px = self.price_grid(points=1200, width_sigma=6.0)
        vals = self.expiry_pl_curve(px)

        zero_bound = self.pl_at_expiry(1e-4)
        vals.append(zero_bound)

        max_profit = max(vals)
        max_loss = min(vals)

        up_slope = self._upside_slope()
        if up_slope > 1e-6:
            max_profit = UNBOUNDED
        elif up_slope < -1e-6:
            max_loss = -UNBOUNDED   # uncovered short call: loss grows forever

        return max_profit, max_loss

    def _upside_slope(self) -> float:
        """dP/L per $1 of underlying, far above spot. Reveals naked short calls."""
        a = self.spot * 8.0
        b = self.spot * 12.0
        dv = self.pl_at_expiry(b) - self.pl_at_expiry(a)
        return dv / (b - a) if b > a else 0.0

    @property
    def is_risk_defined(self) -> bool:
        """
        Structural check, not a curve check: every short option must be covered
        by a long option of the same type (or by stock, for short calls).
        This is what the capital model and the guardrails key off.
        """
        long_calls = sum(l.quantity for l in self.legs
                         if l.kind == "call" and l.quantity > 0)
        short_calls = -sum(l.quantity for l in self.legs
                           if l.kind == "call" and l.quantity < 0)
        long_puts = sum(l.quantity for l in self.legs
                        if l.kind == "put" and l.quantity > 0)
        short_puts = -sum(l.quantity for l in self.legs
                          if l.kind == "put" and l.quantity < 0)
        shares = sum(l.quantity for l in self.legs if l.kind == "stock")

        call_cover = long_calls + max(shares, 0.0) / 100.0
        return short_calls <= call_cover + 1e-9 and short_puts <= long_puts + 1e-9

    # ---- greeks ----------------------------------------------------------

    def net_greeks(self, spot: float | None = None, days_elapsed: float = 0.0,
                   iv_shift: float = 0.0) -> dict:
        s = self.spot if spot is None else spot
        total = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
                 "rho": 0.0, "vanna": 0.0, "charm": 0.0, "vomma": 0.0}
        for leg in self.legs:
            g = leg.greeks_at(s, days_elapsed, self.rate, self.dividend_yield, iv_shift)
            scale = leg.quantity * leg.multiplier
            for k in total:
                total[k] += getattr(g, k, 0.0) * scale
        total["dollar_delta"] = total["delta"] * s
        total["gamma_per_1pct"] = total["gamma"] * (s * 0.01) * s * 0.01 * 0.5
        return total

    def leg_greeks(self, spot: float | None = None) -> list[dict]:
        s = self.spot if spot is None else spot
        rows = []
        for leg in self.legs:
            g = leg.greeks_at(s, 0.0, self.rate, self.dividend_yield)
            scale = leg.quantity * leg.multiplier
            rows.append({
                "label": leg.describe(),
                "kind": leg.kind,
                "strike": leg.strike,
                "dte": leg.dte,
                "quantity": leg.quantity,
                "iv": leg.iv,
                "entry_price": leg.entry_price,
                "theo": g.price,
                "delta": g.delta * scale,
                "gamma": g.gamma * scale,
                "theta": g.theta * scale,
                "vega": g.vega * scale,
                "rho": g.rho * scale,
                "extrinsic": max(g.price - leg.value_at_expiry(s), 0.0) if leg.kind != "stock" else 0.0,
            })
        return rows

    # ---- surface ---------------------------------------------------------

    def pl_surface(self, price_points: int = 41, time_points: int = 25,
                   iv_shift: float = 0.0) -> dict:
        """Price x time P/L grid for the heatmap."""
        prices = self.price_grid(points=price_points)
        horizon = self.min_dte or 30.0
        step = horizon / max(time_points - 1, 1)
        days = [round(i * step, 3) for i in range(time_points)]
        grid = [self.pl_curve(prices, d, iv_shift) for d in days]
        flat = [v for row in grid for v in row]
        return {
            "prices": prices,
            "days": days,
            "grid": grid,
            "min": min(flat) if flat else 0.0,
            "max": max(flat) if flat else 0.0,
        }

    def vol_sensitivity(self, shifts: Sequence[float] = (-0.10, -0.05, 0.0, 0.05, 0.10)) -> list[dict]:
        """P/L at current spot across parallel IV shifts -- the vega story."""
        base = self.pl_at(self.spot, 0.0, 0.0)
        return [
            {"shift": s, "pl": self.pl_at(self.spot, 0.0, s),
             "delta_pl": self.pl_at(self.spot, 0.0, s) - base}
            for s in shifts
        ]
