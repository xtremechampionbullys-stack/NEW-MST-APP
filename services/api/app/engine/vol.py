"""
Volatility surface.

Two implementations behind one interface:

* `ChainVolSurface` interpolates IV from a real option chain. This is what runs
  when Polygon/Massive or Webull data is present.
* `ParametricVolSurface` generates a realistic equity smile -- downside skew,
  term structure, and a smile that flattens with time. This is what lets the
  entire strategy tester run with zero API keys, and it is also the fallback
  for strikes the real chain does not quote.

The surface is the single place that decides "what vol do I price this strike
at", so skew shows up correctly in every payoff curve, greek, and POP number
without any strategy template needing to know about it.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Protocol, Sequence


class VolSurface(Protocol):
    def iv(self, strike: float, dte: float, kind: str = "call") -> float: ...


@dataclass
class ParametricVolSurface:
    """
    SVI-flavoured but deliberately simple: ATM term structure plus a skew term
    in log-moneyness, damped by sqrt(T) so short-dated smiles are steep and
    long-dated ones flatten -- the shape actual equity chains show.
    """
    spot: float
    atm_vol: float = 0.28
    skew: float = 0.18           # downside richness; higher = steeper put skew
    smile: float = 0.55          # curvature (both wings)
    term_slope: float = 0.06     # long-dated vol premium over 30-day
    vol_floor: float = 0.03

    def atm_for(self, dte: float) -> float:
        t = max(dte, 1.0) / 365.0
        anchor = 30.0 / 365.0
        # Contango by default: longer dated carries more vol.
        return max(self.atm_vol + self.term_slope * (math.sqrt(t) - math.sqrt(anchor)),
                   self.vol_floor)

    def iv(self, strike: float, dte: float, kind: str = "call") -> float:
        if strike <= 0 or self.spot <= 0:
            return self.atm_vol
        t = max(dte, 0.5) / 365.0
        k = math.log(strike / self.spot)
        damp = 1.0 / math.sqrt(max(t * 12.0, 0.35))
        v = self.atm_for(dte) - self.skew * k * damp + self.smile * k * k * damp
        return max(v, self.vol_floor)


@dataclass
class ChainVolSurface:
    """
    IV lifted from a live chain. Points are (dte, strike, iv). Bilinear-ish:
    linear in strike within an expiry, linear in total variance across
    expiries, falling back to the parametric surface outside the quoted grid.
    """
    spot: float
    fallback: ParametricVolSurface
    points: dict[float, list[tuple[float, float]]] = field(default_factory=dict)

    def add(self, dte: float, strike: float, iv: float) -> None:
        if iv is None or iv <= 0 or not math.isfinite(iv):
            return
        self.points.setdefault(round(float(dte), 3), []).append((float(strike), float(iv)))

    def finalize(self) -> "ChainVolSurface":
        for d in self.points:
            self.points[d].sort(key=lambda p: p[0])
        return self

    def _iv_in_expiry(self, dte: float, strike: float) -> float | None:
        row = self.points.get(dte)
        if not row:
            return None
        strikes = [p[0] for p in row]
        if strike <= strikes[0]:
            return row[0][1]
        if strike >= strikes[-1]:
            return row[-1][1]
        i = bisect.bisect_left(strikes, strike)
        k0, v0 = row[i - 1]
        k1, v1 = row[i]
        if k1 == k0:
            return v0
        w = (strike - k0) / (k1 - k0)
        return v0 + w * (v1 - v0)

    def iv(self, strike: float, dte: float, kind: str = "call") -> float:
        if not self.points:
            return self.fallback.iv(strike, dte, kind)
        expiries = sorted(self.points.keys())
        target = round(float(dte), 3)

        if target <= expiries[0]:
            v = self._iv_in_expiry(expiries[0], strike)
            return v if v else self.fallback.iv(strike, dte, kind)
        if target >= expiries[-1]:
            v = self._iv_in_expiry(expiries[-1], strike)
            return v if v else self.fallback.iv(strike, dte, kind)

        i = bisect.bisect_left(expiries, target)
        d0, d1 = expiries[i - 1], expiries[i]
        v0 = self._iv_in_expiry(d0, strike)
        v1 = self._iv_in_expiry(d1, strike)
        if v0 is None or v1 is None:
            return self.fallback.iv(strike, dte, kind)

        # Interpolate in total variance, not vol -- avoids calendar arbitrage.
        w0, w1 = v0 * v0 * d0, v1 * v1 * d1
        w = w0 + (w1 - w0) * (target - d0) / (d1 - d0)
        return math.sqrt(max(w, 1e-9) / max(target, 1e-9))


def infer_atm_vol(surface: VolSurface, spot: float, dte: float = 30.0) -> float:
    return surface.iv(spot, dte)


def realized_vol(closes: Sequence[float], window: int = 20,
                 annualize: float = 252.0) -> float | None:
    """Close-to-close realized vol -- the honest comparison for any IV number."""
    if len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * annualize)


def iv_rank(current_iv: float, iv_history: Sequence[float]) -> dict:
    """
    IV Rank and IV Percentile -- the two numbers that decide premium-selling vs
    premium-buying. They disagree more often than people expect, so return both.
    """
    vals = [v for v in iv_history if v and math.isfinite(v)]
    if len(vals) < 5:
        return {"iv_rank": None, "iv_percentile": None,
                "low": None, "high": None}
    lo, hi = min(vals), max(vals)
    rank = (current_iv - lo) / (hi - lo) if hi > lo else 0.5
    pct = sum(1 for v in vals if v < current_iv) / len(vals)
    return {
        "iv_rank": max(0.0, min(1.0, rank)),
        "iv_percentile": pct,
        "low": lo,
        "high": hi,
    }
