"""
The dojo: automated strategy-rule search with honest validation.

The premise is simple -- propose many variations of a rule set, backtest each,
keep what works. The danger is equally simple and it is the reason most
published "optimized" strategies fail live: **if you try two hundred rule sets
on one price history, the best one looks excellent whether or not it has any
edge at all.** Pure noise, searched hard enough, produces a beautiful equity
curve.

So this module is built around three defences, and it reports all three whether
they flatter the result or not:

1. **Walk-forward selection.** Variants are ranked on a training window and
   scored *only* on the window that follows, which the selection never saw. The
   headline number is out-of-sample by construction.

2. **The degradation gap.** In-sample and out-of-sample performance of the same
   selected variant are reported side by side. A large gap is the signature of
   overfitting and is called out explicitly.

3. **Selection skill.** The decisive test, and the one almost nobody runs: does
   picking the in-sample winner beat picking a variant at *random*, measured out
   of sample? If the selected variant's test performance is no better than the
   median variant's test performance, the search learned nothing and the whole
   exercise was noise-mining. This is reported as a percentile.

A dojo run that concludes "no selection skill" has done its job.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import asdict, dataclass, field
from typing import Sequence

import numpy as np

from .backtest import BacktestRules, ModelledQuoteSource, run_backtest


@dataclass
class SearchSpace:
    """Ranges the dojo is allowed to vary. Anything absent stays at the base."""
    profit_target_pct: list[float] = field(
        default_factory=lambda: [0.25, 0.35, 0.5, 0.65, 0.8])
    stop_loss_pct: list[float] = field(
        default_factory=lambda: [1.0, 1.5, 2.0, 3.0, 0.0])   # 0 = disabled
    close_at_dte: list[int] = field(
        default_factory=lambda: [7, 14, 21, 28, 0])          # 0 = disabled
    target_dte: list[int] = field(default_factory=lambda: [30, 45, 60])
    delta: list[float] = field(default_factory=lambda: [0.10, 0.16, 0.22, 0.30])
    entry_every_days: list[int] = field(default_factory=lambda: [5, 7, 14])

    def size(self) -> int:
        return (len(self.profit_target_pct) * len(self.stop_loss_pct)
                * len(self.close_at_dte) * len(self.target_dte)
                * len(self.delta) * len(self.entry_every_days))

    def sample(self, rng: random.Random) -> dict:
        return {
            "profit_target_pct": rng.choice(self.profit_target_pct),
            "stop_loss_pct": rng.choice(self.stop_loss_pct),
            "close_at_dte": rng.choice(self.close_at_dte),
            "target_dte": rng.choice(self.target_dte),
            "delta": rng.choice(self.delta),
            "entry_every_days": rng.choice(self.entry_every_days),
        }

    def to_dict(self) -> dict:
        return asdict(self)


def _apply(base: BacktestRules, variant: dict) -> BacktestRules:
    params = dict(base.params)
    if "delta" in variant:
        params["delta"] = variant["delta"]
    return BacktestRules(
        template_id=base.template_id,
        params=params,
        entry_every_days=int(variant.get("entry_every_days", base.entry_every_days)),
        target_dte=int(variant.get("target_dte", base.target_dte)),
        contracts=base.contracts,
        max_concurrent=base.max_concurrent,
        profit_target_pct=(variant["profit_target_pct"]
                           if variant.get("profit_target_pct") else None),
        stop_loss_pct=(variant["stop_loss_pct"]
                       if variant.get("stop_loss_pct") else None),
        close_at_dte=(int(variant["close_at_dte"])
                      if variant.get("close_at_dte") else None),
        skip_earnings=base.skip_earnings,
        commission_per_contract=base.commission_per_contract,
        slippage_pct=base.slippage_pct,
        variance_risk_premium=base.variance_risk_premium,
    )


def _objective(metrics: dict) -> float:
    """
    What the dojo optimizes.

    Deliberately not total P/L. Maximizing raw profit selects for whatever
    took the most risk in the sample, and on a short history that is almost
    always the variant that got lucky in the tail. This scores risk-adjusted
    return with a penalty for thin samples, so a variant that made money on
    four trades cannot beat one that made money on eighty.
    """
    trades = metrics.get("trades", 0)
    if trades < 8:
        return -1e9
    total = metrics.get("total_pl", 0.0)
    dd = abs(metrics.get("max_drawdown", 0.0)) or 1.0
    sharpe = metrics.get("sharpe") or 0.0
    # Return over max drawdown, blended with Sharpe, shrunk toward zero when
    # the sample is small.
    romad = total / dd
    shrink = trades / (trades + 25.0)
    return shrink * (0.6 * romad + 0.4 * sharpe)


@dataclass
class VariantResult:
    variant: dict
    train: dict
    test: dict
    train_score: float
    test_score: float

    def to_dict(self) -> dict:
        return asdict(self)


def run_dojo(bars: Sequence, base: BacktestRules, *,
             space: SearchSpace | None = None,
             variants: int = 40,
             train_fraction: float = 0.6,
             symbol: str = "UNDL",
             seed: int = 42,
             earnings_dates: Sequence[dt.date] = ()) -> dict:
    """
    Search the rule space, select on train, report on test.

    `variants` is capped deliberately. Every additional variant raises the bar
    the winner must clear to be believable, and the report says how many were
    tried so the reader can discount accordingly.
    """
    if len(bars) < 300:
        raise ValueError(f"dojo needs at least 300 bars; got {len(bars)}")

    # The test slice needs a warm-up prefix so trailing-volatility features are
    # defined on its first trading day -- but that prefix must be EXACTLY the
    # backtester's warm-up length, no more. Any extra and the test window starts
    # trading before the training window ends, which silently leaks training
    # data into the "out-of-sample" score and makes the whole validation a lie.
    warmup = 60
    split = int(len(bars) * train_fraction)
    train_bars = bars[:split]
    test_bars = bars[split - warmup:]
    if len(test_bars) < warmup + 150:
        raise ValueError("not enough history left for an out-of-sample window")

    train_trading_end = train_bars[-1].t.date()
    test_trading_start = test_bars[warmup].t.date()
    if test_trading_start < train_trading_end:
        raise ValueError(
            f"train/test overlap: test trading begins {test_trading_start} but "
            f"training runs to {train_trading_end}")

    rng = random.Random(seed)
    space = space or SearchSpace()

    seen: set[tuple] = set()
    results: list[VariantResult] = []

    for _ in range(variants * 4):
        if len(results) >= variants:
            break
        v = space.sample(rng)
        key = tuple(sorted(v.items()))
        if key in seen:
            continue
        seen.add(key)

        rules = _apply(base, v)
        try:
            tr = run_backtest(train_bars, rules, symbol=symbol,
                              earnings_dates=earnings_dates)
            te = run_backtest(test_bars, rules, symbol=symbol,
                              earnings_dates=earnings_dates)
        except (ValueError, KeyError):
            continue

        results.append(VariantResult(
            variant=v, train=tr["metrics"], test=te["metrics"],
            train_score=_objective(tr["metrics"]),
            test_score=_objective(te["metrics"]),
        ))

    if not results:
        raise ValueError("no variant produced a usable backtest")

    ranked = sorted(results, key=lambda r: r.train_score, reverse=True)
    selected = ranked[0]

    test_scores = np.array([r.test_score for r in results])
    valid = test_scores[test_scores > -1e8]
    median_test = float(np.median(valid)) if valid.size else 0.0
    percentile = (float((valid < selected.test_score).mean() * 100)
                  if valid.size else 50.0)

    # Baseline: the base rule set itself, scored on the same test window. If
    # the search cannot beat the rules you started with, it has cost you time
    # and added risk of overfitting for nothing.
    try:
        base_test = run_backtest(test_bars, base, symbol=symbol,
                                 earnings_dates=earnings_dates)["metrics"]
        base_test_score = _objective(base_test)
    except (ValueError, KeyError):
        base_test, base_test_score = None, None

    degradation = selected.train_score - selected.test_score
    beat_random = percentile > 60
    beat_base = (base_test_score is not None
                 and selected.test_score > base_test_score)

    if not beat_random:
        verdict = (
            f"No selection skill. The variant that ranked first in training landed at "
            f"the {percentile:.0f}th percentile out of sample — picking at random from "
            f"the {len(results)} variants tried would have done about as well. The "
            f"in-sample ranking was noise. Do not deploy this."
        )
    elif degradation > abs(selected.train_score) * 0.5:
        verdict = (
            f"Selection beat random ({percentile:.0f}th percentile out of sample), but "
            f"performance degraded sharply from training to test — the classic overfitting "
            f"signature. Treat the out-of-sample figures as the real ones and expect live "
            f"results at or below them."
        )
    else:
        verdict = (
            f"The selected variant reached the {percentile:.0f}th percentile out of sample "
            f"with modest degradation from training. That is what a genuine, if small, edge "
            f"looks like. It was still chosen from {len(results)} candidates on one price "
            f"history, so size it as a hypothesis, not a conclusion."
        )

    return {
        "symbol": symbol.upper(),
        "template": base.template_id,
        "variants_tried": len(results),
        "search_space_size": space.size(),
        # Reported as TRADING windows, not slice boundaries. The test slice
        # carries a warm-up prefix that is never traded, and printing that as
        # the start date would make the two windows look like they overlap.
        "train_window": {"start": train_bars[warmup].t.date().isoformat(),
                         "end": train_trading_end.isoformat(),
                         "bars": len(train_bars) - warmup},
        "test_window": {"start": test_trading_start.isoformat(),
                        "end": test_bars[-1].t.date().isoformat(),
                        "bars": len(test_bars) - warmup},
        "selected": {
            "variant": selected.variant,
            "train_metrics": selected.train,
            "test_metrics": selected.test,
            "train_score": round(selected.train_score, 4),
            "test_score": round(selected.test_score, 4),
        },
        "baseline": {
            "rules": {"profit_target_pct": base.profit_target_pct,
                      "stop_loss_pct": base.stop_loss_pct,
                      "close_at_dte": base.close_at_dte,
                      "target_dte": base.target_dte},
            "test_metrics": base_test,
            "test_score": round(base_test_score, 4) if base_test_score is not None else None,
        },
        "validation": {
            "out_of_sample_percentile": round(percentile, 1),
            "median_variant_test_score": round(median_test, 4),
            "degradation": round(degradation, 4),
            "selection_beat_random": beat_random,
            "selection_beat_baseline": beat_base,
        },
        "leaderboard": [
            {**r.variant,
             "train_pl": r.train.get("total_pl"), "test_pl": r.test.get("total_pl"),
             "train_trades": r.train.get("trades"), "test_trades": r.test.get("trades"),
             "train_win_rate": r.train.get("win_rate"), "test_win_rate": r.test.get("win_rate"),
             "train_score": round(r.train_score, 3), "test_score": round(r.test_score, 3),
             "test_max_drawdown": r.test.get("max_drawdown")}
            for r in ranked[:25]
        ],
        "verdict": verdict,
        "caveat": (
            f"{len(results)} rule sets were tested against the same price history. "
            f"With that many attempts, the best in-sample result would look strong "
            f"even on pure noise — which is exactly why the out-of-sample percentile "
            f"above, not the leaderboard, is the number that matters. All pricing is "
            f"modelled, so this validates the RULES, not the fills."
        ),
    }
