"""
Forward testing: Monte Carlo projection and paper-trade tracking.

Backtesting asks "what would this have done". Forward testing asks the two
questions that actually precede a trade and follow it:

1. **What could happen from here?** A Monte Carlo over simulated price paths,
   marking the position each step. Unlike the closed-form analytics in
   analytics.py, this is path-dependent -- it can answer whether a profit target
   would have been hit *before* a stop, how deep the position goes underwater on
   the way to a win, and how long it takes. Those are management questions and
   closed forms cannot answer them.

2. **Is the model actually right?** Paper-trade tracking records what the engine
   predicted at entry and compares it to what happened. A model that has never
   been scored against reality is a hypothesis, not a tool. This is the only
   part of the whole system that can eventually falsify the rest of it.

The simulation is deliberately richer than geometric Brownian motion:
volatility mean-reverts (calibrated by GARCH), returns are fat-tailed via a
Student-t innovation, and scheduled events inject a discrete jump. GBM would
systematically understate the frequency of the moves that actually damage short
premium positions.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Sequence

import numpy as np

from .payoff import Position
from .pricing import DAYS_PER_YEAR


@dataclass
class SimulationConfig:
    paths: int = 4000
    steps_per_day: int = 1
    horizon_days: int | None = None      # defaults to the position's near expiry
    annual_drift: float = 0.0            # risk-neutral by default
    vol_of_vol: float = 0.6              # GARCH-style vol clustering strength
    vol_mean_reversion: float = 0.06     # per-day pull back to the long-run level
    tail_df: float = 4.0                 # Student-t degrees of freedom; None = normal
    jump_days: list[int] = field(default_factory=list)
    jump_vol: float = 0.0                # one-off event return sd
    seed: int = 12345

    def to_dict(self) -> dict:
        return asdict(self)


def simulate_paths(spot: float, base_vol: float, cfg: SimulationConfig,
                   horizon_days: int) -> np.ndarray:
    """
    Price paths, shape (paths, horizon_days + 1).

    Student-t innovations rescaled to unit variance, so raising the tail weight
    fattens the tails without secretly raising overall volatility -- otherwise
    every "fat tail" comparison would be confounded by a vol change.
    """
    rng = np.random.default_rng(cfg.seed)
    n, h = cfg.paths, horizon_days
    dt_y = 1.0 / DAYS_PER_YEAR

    if cfg.tail_df and cfg.tail_df > 2:
        raw = rng.standard_t(cfg.tail_df, size=(n, h))
        raw /= math.sqrt(cfg.tail_df / (cfg.tail_df - 2))
    else:
        raw = rng.standard_normal(size=(n, h))

    log_paths = np.zeros((n, h + 1))
    log_paths[:, 0] = math.log(spot)

    var = np.full(n, (base_vol ** 2) * dt_y)
    long_run = (base_vol ** 2) * dt_y
    drift_step = (cfg.annual_drift * dt_y)

    jump_set = set(cfg.jump_days)
    for t in range(1, h + 1):
        sd = np.sqrt(var)
        shock = raw[:, t - 1] * sd
        log_paths[:, t] = log_paths[:, t - 1] + drift_step - 0.5 * var + shock

        if t in jump_set and cfg.jump_vol > 0:
            j = rng.standard_normal(n) * cfg.jump_vol
            log_paths[:, t] += j - 0.5 * cfg.jump_vol ** 2

        # GARCH-style update: today's surprise feeds tomorrow's variance, with
        # a pull back toward the long-run level.
        var = (long_run * cfg.vol_mean_reversion
               + (1 - cfg.vol_mean_reversion)
               * (var + cfg.vol_of_vol * (shock ** 2 - var)))
        var = np.clip(var, long_run * 0.05, long_run * 25)

    return np.exp(log_paths)


@dataclass
class ManagementRules:
    profit_target_pct: float | None = 0.50   # fraction of entry basis
    stop_loss_pct: float | None = 2.00       # multiple of entry basis
    close_at_dte: int | None = 21

    def to_dict(self) -> dict:
        return asdict(self)


def forward_test(position: Position, *, config: SimulationConfig | None = None,
                 rules: ManagementRules | None = None,
                 base_vol: float | None = None,
                 event_days: Sequence[int] = (),
                 event_jump_vol: float = 0.0) -> dict:
    """
    Project a position forward and apply management rules along each path.

    The headline number people expect is the probability of profit, which the
    closed-form analytics already give. The numbers that are only available
    here are the path-dependent ones: how often the stop is hit before the
    target, how far underwater a winning trade goes first, and how long capital
    is committed. Those decide whether a strategy is survivable, as opposed to
    merely positive-expectancy.
    """
    cfg = config or SimulationConfig()
    rul = rules or ManagementRules()
    # Only None means "use the position's expiry". An explicitly supplied 0 or
    # negative is a caller error and must surface as one rather than silently
    # becoming a 45-day run that the caller never asked for.
    horizon = int(cfg.horizon_days if cfg.horizon_days is not None
                  else (position.min_dte or 30))
    if horizon < 1:
        raise ValueError(
            f"horizon must be at least one day; got {cfg.horizon_days}")

    vol = base_vol if base_vol is not None else position.blended_iv
    vol = max(float(vol or 0.3), 0.02)

    cfg = SimulationConfig(**{**cfg.to_dict(),
                              "jump_days": list(event_days),
                              "jump_vol": event_jump_vol})
    paths = simulate_paths(position.spot, vol, cfg, horizon)

    basis = abs(position.net_debit) if abs(position.net_debit) > 1e-9 else 1.0
    target = rul.profit_target_pct * basis if rul.profit_target_pct else None
    stop = -rul.stop_loss_pct * basis if rul.stop_loss_pct else None
    close_day = (horizon - rul.close_at_dte) if rul.close_at_dte is not None else horizon
    close_day = max(1, min(close_day, horizon))

    n = paths.shape[0]
    outcome_pl = np.zeros(n)
    exit_day = np.full(n, horizon, dtype=int)
    reason = np.zeros(n, dtype=int)     # 0 held, 1 target, 2 stop, 3 time exit
    mae = np.zeros(n)
    mfe = np.zeros(n)
    open_mask = np.ones(n, dtype=bool)

    # Mark on a coarse ladder of days: pricing every path every day is the
    # expensive part, and a position's P/L does not change enough between
    # adjacent days to justify it.
    mark_days = sorted(set(list(range(1, horizon + 1, max(1, horizon // 30))) + [close_day, horizon]))

    for day in mark_days:
        if not open_mask.any():
            break
        spots = paths[:, day]
        pl = np.array([position.pl_at(float(s), day) for s in spots]) \
            if n <= 400 else _vectorized_pl(position, spots, day)

        mae = np.where(open_mask, np.minimum(mae, pl), mae)
        mfe = np.where(open_mask, np.maximum(mfe, pl), mfe)

        hit_target = open_mask & (target is not None) & (pl >= (target if target else np.inf))
        hit_stop = open_mask & (stop is not None) & (pl <= (stop if stop else -np.inf))
        # A path that crosses both between marks is resolved conservatively:
        # the stop is assumed to trigger first. Assuming the target won would
        # flatter every result.
        hit_target = hit_target & ~hit_stop

        for mask, code in ((hit_stop, 2), (hit_target, 1)):
            if mask.any():
                outcome_pl[mask] = pl[mask]
                exit_day[mask] = day
                reason[mask] = code
                open_mask[mask] = False

        if day >= close_day and open_mask.any():
            outcome_pl[open_mask] = pl[open_mask]
            exit_day[open_mask] = day
            reason[open_mask] = 3
            open_mask[:] = False

    if open_mask.any():
        spots = paths[:, horizon]
        pl = _vectorized_pl(position, spots, horizon)
        outcome_pl[open_mask] = pl[open_mask]
        exit_day[open_mask] = horizon
        reason[open_mask] = 3

    wins = outcome_pl > 0
    pct = lambda p: float(np.percentile(outcome_pl, p))

    return {
        "horizon_days": horizon,
        "paths": n,
        "base_vol": vol,
        "config": cfg.to_dict(),
        "rules": rul.to_dict(),
        "entry_basis": position.net_debit,
        "outcomes": {
            "win_rate": float(wins.mean()),
            "mean_pl": float(outcome_pl.mean()),
            "median_pl": pct(50),
            "p05": pct(5), "p25": pct(25), "p75": pct(75), "p95": pct(95),
            "best": float(outcome_pl.max()), "worst": float(outcome_pl.min()),
            "expected_win": float(outcome_pl[wins].mean()) if wins.any() else None,
            "expected_loss": float(outcome_pl[~wins].mean()) if (~wins).any() else None,
        },
        "path_dependent": {
            "hit_profit_target": float((reason == 1).mean()),
            "hit_stop_loss": float((reason == 2).mean()),
            "closed_on_time": float((reason == 3).mean()),
            "mean_days_held": float(exit_day.mean()),
            "median_days_held": float(np.median(exit_day)),
            "mean_max_adverse": float(mae.mean()),
            "worst_max_adverse": float(mae.min()),
            # The number that most often surprises people: how far a WINNING
            # trade goes underwater before it works.
            "mean_max_adverse_on_winners": float(mae[wins].mean()) if wins.any() else None,
            "mean_max_favorable": float(mfe.mean()),
        },
        "terminal_prices": {
            "p05": float(np.percentile(paths[:, horizon], 5)),
            "p25": float(np.percentile(paths[:, horizon], 25)),
            "median": float(np.percentile(paths[:, horizon], 50)),
            "p75": float(np.percentile(paths[:, horizon], 75)),
            "p95": float(np.percentile(paths[:, horizon], 95)),
        },
        "fan_chart": _fan_chart(paths),
        "pl_histogram": _histogram(outcome_pl),
        "note": ("Path-dependent, so the target/stop probabilities account for "
                 "the ORDER events happen in. Where a path would cross both "
                 "between marks the stop is assumed to trigger first — the "
                 "conservative assumption, since the flattering one is what "
                 "makes simulations lie."),
    }


def _vectorized_pl(position: Position, spots: np.ndarray, days: float) -> np.ndarray:
    """
    Position P/L across many spots at one point in time.

    Prices each leg once across the whole spot vector instead of once per path,
    which is what makes a few thousand paths tractable in a request.
    """
    from .pricing import bs_price, intrinsic, years_from_dte

    total = np.zeros(spots.shape[0])
    for leg in position.legs:
        scale = leg.quantity * leg.multiplier
        if leg.kind == "stock":
            total += spots * scale
            continue
        remaining = leg.dte - days
        if remaining <= 0:
            vals = (np.maximum(spots - leg.strike, 0) if leg.kind == "call"
                    else np.maximum(leg.strike - spots, 0))
        else:
            T = years_from_dte(remaining)
            vals = np.array([bs_price(float(s), float(leg.strike), T, position.rate,
                                      position.dividend_yield, leg.iv, leg.kind)
                             for s in spots])
        total += vals * scale
    return total - position.net_debit


def _fan_chart(paths: np.ndarray, bands: Sequence[int] = (5, 25, 50, 75, 95)) -> dict:
    steps = paths.shape[1]
    stride = max(1, steps // 40)
    days = list(range(0, steps, stride))
    if days[-1] != steps - 1:
        days.append(steps - 1)
    return {
        "days": days,
        "bands": {str(b): [float(np.percentile(paths[:, d], b)) for d in days]
                  for b in bands},
    }


def _histogram(values: np.ndarray, buckets: int = 24, clip_pct: float = 1.0) -> dict:
    """
    P/L distribution, with the tails held out of the binning range.

    A short premium position produces exactly the distribution that breaks a
    naive histogram: ninety-odd percent of paths land within a few hundred
    dollars of max profit, and a handful land thousands of dollars away. Binning
    min-to-max puts every one of those thousands of paths in a single bar and
    renders the interesting structure as a flat line at zero -- the chart says
    "one outcome happens" when the truth is "one outcome usually happens and
    the other one is what kills you".

    So the bins span the 1st to 99th percentile and everything outside is
    reported separately, with its own counts and its actual extremes. The tail
    is not dropped -- it is promoted from a one-pixel bar to a labelled fact,
    which is the opposite of hiding it.
    """
    lo_full, hi_full = float(values.min()), float(values.max())
    if abs(hi_full - lo_full) < 1e-9:
        return {"edges": [lo_full, hi_full], "counts": [int(values.size)],
                "below": 0, "above": 0, "min": lo_full, "max": hi_full,
                "clipped": False}

    lo = float(np.percentile(values, clip_pct))
    hi = float(np.percentile(values, 100.0 - clip_pct))
    if hi - lo < (hi_full - lo_full) * 1e-6:
        lo, hi = lo_full, hi_full

    inside = values[(values >= lo) & (values <= hi)]
    counts, edges = np.histogram(inside, bins=buckets, range=(lo, hi))
    below = int((values < lo).sum())
    above = int((values > hi).sum())
    return {
        "edges": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
        "below": below,
        "above": above,
        "min": lo_full,
        "max": hi_full,
        "clipped": bool(below or above),
    }


# --------------------------------------------------------------------------
# Paper trades
# --------------------------------------------------------------------------

@dataclass
class PaperTradeExpectation:
    """
    What the engine predicted at entry, frozen.

    Stored at entry and never recomputed. The whole value of a paper-trade log
    is comparing a prediction to an outcome, and a prediction that gets quietly
    updated as evidence arrives is not a prediction.
    """
    probability_of_profit: float
    expected_value: float
    max_profit: float | None
    max_loss: float | None
    breakevens: list[float]
    expected_move_pct: float
    blended_iv: float
    forward_test: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def score_paper_trade(expectation: dict, realized_pl: float,
                      realized_days: int) -> dict:
    """
    Compare an outcome to what was predicted at entry.

    A single trade proves nothing -- the correct reading of one loss on a 70%
    trade is "that happens 30% of the time". These fields exist to be aggregated
    across many trades, where calibration becomes measurable: if trades entered
    at a 70% predicted win rate win about 70% of the time, the model is
    calibrated, whatever any individual result did.
    """
    pop = expectation.get("probability_of_profit")
    ev = expectation.get("expected_value")
    won = realized_pl > 0
    return {
        "won": won,
        "realized_pl": realized_pl,
        "days_held": realized_days,
        "predicted_pop": pop,
        "predicted_ev": ev,
        "ev_error": (realized_pl - ev) if ev is not None else None,
        # Brier score of the single binary prediction. Only meaningful in
        # aggregate; kept per-trade so the aggregate is a plain mean.
        "brier": ((1.0 if won else 0.0) - pop) ** 2 if pop is not None else None,
        "note": "One trade cannot validate a probability. Aggregate before concluding.",
    }


def calibration_report(scored: Sequence[dict], buckets: int = 5) -> dict:
    """
    Are the predicted probabilities honest?

    Bucket trades by predicted win probability and compare each bucket's
    predicted rate to its realized rate. This is the single most useful thing a
    paper-trade log produces: it validates or falsifies the probability engine
    itself, rather than just tallying profit.
    """
    rows = [s for s in scored if s.get("predicted_pop") is not None]
    if len(rows) < 5:
        return {"available": False,
                "reason": f"only {len(rows)} scored trades; calibration needs a few dozen",
                "trades": len(rows)}

    edges = np.linspace(0, 1, buckets + 1)
    out = []
    for i in range(buckets):
        lo, hi = edges[i], edges[i + 1]
        group = [r for r in rows if lo <= r["predicted_pop"] < hi
                 or (i == buckets - 1 and r["predicted_pop"] == 1.0)]
        if not group:
            continue
        predicted = sum(r["predicted_pop"] for r in group) / len(group)
        realized = sum(1 for r in group if r["won"]) / len(group)
        out.append({
            "bucket": f"{lo:.0%}–{hi:.0%}",
            "trades": len(group),
            "predicted_win_rate": round(predicted, 4),
            "realized_win_rate": round(realized, 4),
            "gap": round(realized - predicted, 4),
        })

    brier = float(np.mean([r["brier"] for r in rows if r["brier"] is not None]))
    base = sum(1 for r in rows if r["won"]) / len(rows)
    # The score a model would get by ignoring every input and predicting the
    # overall realized win rate for every trade. For a constant forecast p=base
    # the Brier score reduces exactly to base*(1-base). Beating this is the
    # minimum bar for the probability engine to be worth anything.
    brier_base = base * (1.0 - base)

    return {
        "available": True,
        "trades": len(rows),
        "buckets": out,
        "brier": round(brier, 4),
        "brier_base_rate": round(brier_base, 4),
        "better_than_base_rate": brier < brier_base,
        "total_ev_error": round(sum(r["ev_error"] or 0 for r in rows), 2),
        "note": ("A calibrated model's predicted and realized win rates match "
                 "within each bucket. Systematic gaps mean the probability "
                 "engine is wrong, regardless of whether the account is up."),
    }
