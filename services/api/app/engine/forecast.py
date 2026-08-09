"""
Forecasting: volatility, direction, and cross-asset propagation.

A word on what is and is not forecastable, because the two live in this file
side by side and conflating them is how people lose money.

**Volatility is genuinely forecastable.** It clusters, mean-reverts, and has a
long memory. HAR-RV (Corsi 2009) -- regressing tomorrow's realized volatility
on its own daily, weekly and monthly averages -- routinely achieves out-of-
sample R^2 in the 0.4-0.6 range on equity indices. That is a real, replicable
edge and it is what the term-structure projection here rests on.

**Short-horizon direction is very nearly not forecastable.** Decades of
literature put the achievable out-of-sample information coefficient for daily
or weekly equity direction at close to zero, and the overwhelming majority of
published directional systems are overfit. This module builds a directional
model anyway -- it was asked for, and there is a defensible weak-signal version
of it (cross-sectional momentum, short-horizon reversal, volatility-regime
conditioning). But it is wired so that it CANNOT report an in-sample number:
every score it emits comes from walk-forward, out-of-sample evaluation, and it
reports its skill next to the naive base rate so a coin flip cannot look like
insight.

If the measured edge is nil, the response says so. That is the feature.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Sequence

import numpy as np

TRADING_DAYS = 252


# --------------------------------------------------------------------------
# Return and volatility utilities
# --------------------------------------------------------------------------

def log_returns(closes: Sequence[float]) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    c = c[c > 0]
    if c.size < 2:
        return np.array([])
    return np.diff(np.log(c))


def garman_klass_variance(bars: Sequence) -> np.ndarray:
    """
    Daily variance from the open-high-low-close range (Garman-Klass 1980).

        sigma^2 = 0.5 * ln(H/L)^2 - (2 ln2 - 1) * ln(C/O)^2

    A single squared close-to-close return is an unbiased but extremely noisy
    variance estimator -- its own standard error is larger than the quantity it
    estimates. Range estimators use the whole day's path and are roughly five to
    seven times more efficient, which is the difference between HAR finding the
    volatility signal and drowning in it.
    """
    out = []
    two_ln2_minus_1 = 2 * math.log(2) - 1
    for b in bars:
        o, h, l, c = float(b.o), float(b.h), float(b.l), float(b.c)
        if min(o, h, l, c) <= 0 or h < l:
            out.append(np.nan)
            continue
        hl = math.log(h / l) ** 2
        co = math.log(c / o) ** 2
        var = 0.5 * hl - two_ln2_minus_1 * co
        out.append(max(var, 1e-10))
    arr = np.asarray(out, dtype=float)
    if np.isnan(arr).any():
        med = float(np.nanmedian(arr)) if not np.isnan(arr).all() else 1e-8
        arr = np.where(np.isnan(arr), med, arr)
    return arr


def realized_vol_series(returns: np.ndarray, window: int = 21) -> np.ndarray:
    """Rolling annualized realized volatility, aligned to the END of each window."""
    if returns.size < window:
        return np.array([])
    out = np.empty(returns.size - window + 1)
    for i in range(out.size):
        out[i] = returns[i:i + window].std(ddof=1) * math.sqrt(TRADING_DAYS)
    return out


# --------------------------------------------------------------------------
# HAR-RV volatility model
# --------------------------------------------------------------------------

@dataclass
class HARForecast:
    horizon_days: int
    forecast_vol: float
    daily_component: float
    weekly_component: float
    monthly_component: float
    coefficients: dict
    in_sample_r2: float
    out_of_sample_r2: float | None
    observations: int
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Least squares with an intercept, via lstsq (rank-safe)."""
    A = np.column_stack([np.ones(X.shape[0]), X])
    beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    return beta


def _r2(y: np.ndarray, pred: np.ndarray) -> float:
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0


def har_rv_forecast(closes: Sequence[float], horizon_days: int = 21,
                    test_fraction: float = 0.3,
                    bars: Sequence | None = None) -> HARForecast | None:
    """
    Heterogeneous Autoregressive model of Realized Volatility (Corsi, 2009).

        log RV_{t+h} = c + b_d * log RV_t^(d) + b_w * log RV_t^(w) + b_m * log RV_t^(m)

    Fitted in logs, because realized volatility is right-skewed and roughly
    log-normal; fitting in levels lets a single spike dominate the regression.

    The out-of-sample R^2 is computed on a held-out tail the model never saw.
    That number, not the in-sample fit, is the one worth believing.
    """
    rets = log_returns(closes)
    if rets.size < 120:
        return None

    # Prefer the range estimator when OHLC is available -- far less noisy than
    # squared close-to-close, and the noise is what usually defeats HAR on
    # daily data.
    if bars is not None and len(bars) >= rets.size:
        rv_daily = np.sqrt(garman_klass_variance(bars)[-rets.size:] * TRADING_DAYS)
    else:
        rv_daily = np.abs(rets) * math.sqrt(TRADING_DAYS)
    rv_daily = np.maximum(rv_daily, 1e-4)

    def rolling_mean(a: np.ndarray, w: int) -> np.ndarray:
        c = np.cumsum(np.insert(a, 0, 0.0))
        return (c[w:] - c[:-w]) / w

    d_lag, w_lag, m_lag = 1, 5, 22
    n = rv_daily.size
    start = m_lag
    end = n - horizon_days
    if end - start < 60:
        return None

    rows_d, rows_w, rows_m, target = [], [], [], []
    for t in range(start, end):
        rows_d.append(rv_daily[t - d_lag:t].mean())
        rows_w.append(rv_daily[t - w_lag:t].mean())
        rows_m.append(rv_daily[t - m_lag:t].mean())
        # Target: average realized vol over the NEXT horizon days.
        target.append(rv_daily[t:t + horizon_days].mean())

    X = np.log(np.column_stack([rows_d, rows_w, rows_m]))
    y = np.log(np.asarray(target))

    split = int(len(y) * (1 - test_fraction))
    if split < 40 or len(y) - split < 20:
        beta = _ols(X, y)
        pred_in = np.column_stack([np.ones(X.shape[0]), X]) @ beta
        oos = None
    else:
        beta_tr = _ols(X[:split], y[:split])
        pred_te = np.column_stack([np.ones(X.shape[0] - split), X[split:]]) @ beta_tr
        oos = _r2(y[split:], pred_te)
        beta = _ols(X, y)
        pred_in = np.column_stack([np.ones(X.shape[0]), X]) @ beta

    r2_in = _r2(y, pred_in)

    latest = np.log([
        rv_daily[-d_lag:].mean(),
        rv_daily[-w_lag:].mean(),
        rv_daily[-m_lag:].mean(),
    ])
    forecast = float(np.exp(beta[0] + beta[1:] @ latest))

    note = ("Volatility is one of the few genuinely forecastable quantities in "
            "markets; treat this number as a real estimate, bounded by the "
            "out-of-sample R-squared shown.")
    if oos is not None and oos < 0.05:
        note = ("Out-of-sample R-squared is near zero on this series, so the "
                "model is not beating a constant. Use the trailing realized "
                "volatility instead and disregard the projection.")

    return HARForecast(
        horizon_days=horizon_days,
        forecast_vol=forecast,
        daily_component=float(np.exp(latest[0])),
        weekly_component=float(np.exp(latest[1])),
        monthly_component=float(np.exp(latest[2])),
        coefficients={"const": float(beta[0]), "daily": float(beta[1]),
                      "weekly": float(beta[2]), "monthly": float(beta[3])},
        in_sample_r2=r2_in,
        out_of_sample_r2=oos,
        observations=int(len(y)),
        note=note,
    )


# --------------------------------------------------------------------------
# GARCH(1,1)
# --------------------------------------------------------------------------

@dataclass
class GarchFit:
    omega: float
    alpha: float
    beta: float
    persistence: float
    long_run_vol: float
    current_vol: float
    half_life_days: float | None
    log_likelihood: float
    converged: bool

    def to_dict(self) -> dict:
        return asdict(self)

    def project(self, horizon_days: int) -> list[dict]:
        """
        Term structure of expected variance.

        GARCH mean-reverts to its long-run level at rate (alpha+beta) per day,
        which is exactly why an implied-volatility term structure slopes the way
        it does: high spot vol projects downward, low spot vol projects upward.
        """
        lr_var = (self.long_run_vol ** 2) / TRADING_DAYS
        var = (self.current_vol ** 2) / TRADING_DAYS
        out = []
        cum = 0.0
        for h in range(1, horizon_days + 1):
            var = lr_var + self.persistence * (var - lr_var)
            cum += var
            out.append({
                "day": h,
                "daily_vol": math.sqrt(max(var, 1e-12)) * math.sqrt(TRADING_DAYS),
                # Average vol over [0, h] -- the quantity an option of that
                # maturity is actually priced off.
                "average_vol_to_date": math.sqrt(max(cum / h, 1e-12)) * math.sqrt(TRADING_DAYS),
            })
        return out


def fit_garch(closes: Sequence[float]) -> GarchFit | None:
    """
    GARCH(1,1) by maximum likelihood with variance targeting.

    Variance targeting pins omega to the sample variance, which removes one
    parameter and makes the fit far more stable on the few hundred observations
    a retail-length history provides. The remaining two parameters are found by
    a bounded grid plus local refinement -- slower than a gradient method but it
    cannot wander into the non-stationary region, which is the usual failure.
    """
    rets = log_returns(closes)
    if rets.size < 100:
        return None
    r = rets - rets.mean()
    sample_var = float(np.var(r, ddof=1))
    if sample_var <= 0:
        return None

    def nll(alpha: float, beta: float) -> float:
        if alpha <= 0 or beta <= 0 or alpha + beta >= 0.999:
            return 1e12
        omega = sample_var * (1 - alpha - beta)
        var = sample_var
        total = 0.0
        for x in r:
            total += math.log(var) + (x * x) / var
            var = omega + alpha * x * x + beta * var
            if var <= 0 or not math.isfinite(var):
                return 1e12
        return 0.5 * total

    best = (1e12, 0.08, 0.90)
    for a in np.linspace(0.01, 0.30, 16):
        for b in np.linspace(0.50, 0.98, 20):
            if a + b >= 0.999:
                continue
            v = nll(float(a), float(b))
            if v < best[0]:
                best = (v, float(a), float(b))

    _, alpha, beta = best
    for scale in (0.02, 0.005):
        improved = True
        while improved:
            improved = False
            for da in (-scale, 0, scale):
                for db in (-scale, 0, scale):
                    a2, b2 = alpha + da, beta + db
                    if a2 <= 0 or b2 <= 0 or a2 + b2 >= 0.999:
                        continue
                    v = nll(a2, b2)
                    if v < best[0] - 1e-9:
                        best, alpha, beta, improved = (v, a2, b2), a2, b2, True

    omega = sample_var * (1 - alpha - beta)
    var = sample_var
    for x in r:
        var = omega + alpha * x * x + beta * var
    persistence = alpha + beta
    half_life = (math.log(0.5) / math.log(persistence)) if 0 < persistence < 1 else None

    return GarchFit(
        omega=omega, alpha=alpha, beta=beta, persistence=persistence,
        long_run_vol=math.sqrt(sample_var * TRADING_DAYS),
        current_vol=math.sqrt(max(var, 1e-12) * TRADING_DAYS),
        half_life_days=half_life,
        log_likelihood=-best[0],
        converged=best[0] < 1e11,
    )


# --------------------------------------------------------------------------
# Volatility term-structure projection
# --------------------------------------------------------------------------

def project_vol_to_dates(closes: Sequence[float], target_dates: Sequence[dt.date],
                         *, today: dt.date | None = None,
                         implied_now: float | None = None,
                         event_dates: Sequence[dt.date] = ()) -> dict:
    """
    Expected average volatility between now and each target date.

    Three ingredients, combined explicitly rather than blended into one opaque
    number:

    1. GARCH mean reversion supplies the shape of the term structure.
    2. HAR supplies the level, since it forecasts realized volatility better.
    3. Scheduled events add jump variance on top, additively, for any date that
       falls before the target -- the same model the earnings engine uses, so
       the two agree with each other.

    Where implied volatility is supplied, the response also reports the spread
    between projected realized and current implied. That difference, not either
    level on its own, is what a premium seller is actually trading.
    """
    today = today or dt.date.today()
    garch = fit_garch(closes)
    har = har_rv_forecast(closes, horizon_days=21)

    if garch is None:
        return {"available": False,
                "reason": "not enough price history to fit a volatility model"}

    max_days = max((d - today).days for d in target_dates) if target_dates else 30
    path = garch.project(max(max_days, 1))

    # HAR is the better level forecast; GARCH is the better shape. Rescale the
    # GARCH path so its 21-day average matches HAR, keeping the term structure.
    scale = 1.0
    if har and har.out_of_sample_r2 is not None and har.out_of_sample_r2 > 0.05:
        idx = min(21, len(path)) - 1
        garch_21 = path[idx]["average_vol_to_date"]
        if garch_21 > 1e-9:
            scale = har.forecast_vol / garch_21

    rows = []
    for d in sorted(target_dates):
        days = (d - today).days
        if days <= 0:
            continue
        base = path[min(days, len(path)) - 1]["average_vol_to_date"] * scale
        events = [e for e in event_dates if today < e <= d]
        total_var = base * base
        if events:
            # Event variance is a fixed quantity spread over the life of the
            # option: sigma^2 += jump^2 / T. Same additive model as the
            # earnings engine, so a projection and an implied move agree.
            t = days / 365.0
            # 6% is a neutral placeholder when no per-name jump estimate exists.
            for _ in events:
                total_var += (0.06 ** 2) / max(t, 1e-6)
        projected = math.sqrt(total_var)
        rows.append({
            "date": d.isoformat(),
            "days": days,
            "projected_vol": round(projected, 5),
            "diffusive_vol": round(base, 5),
            "events_in_window": len(events),
            "implied_now": implied_now,
            "implied_minus_projected": (round(implied_now - projected, 5)
                                        if implied_now is not None else None),
        })

    return {
        "available": True,
        "as_of": today.isoformat(),
        "garch": garch.to_dict(),
        "har": har.to_dict() if har else None,
        "level_scale_applied": round(scale, 4),
        "projections": rows,
        "note": ("Shape from GARCH mean reversion, level from HAR where its "
                 "out-of-sample fit justifies it, plus additive event variance. "
                 "Volatility genuinely mean-reverts, which is what makes this "
                 "projection meaningful in a way a directional one is not."),
    }


# --------------------------------------------------------------------------
# Directional model
# --------------------------------------------------------------------------

FEATURES = [
    "mom_21", "mom_63", "mom_126", "reversal_5", "vol_ratio",
    "dist_from_ma50", "rsi2", "trend",
]


def _build_features(closes: np.ndarray) -> tuple[np.ndarray, list[int]]:
    """
    Feature matrix aligned to each index t, using ONLY data up to and including
    t. Any feature that peeked at t+1 would make the whole exercise worthless,
    so the construction is deliberately explicit rather than vectorized-clever.
    """
    n = closes.size
    rows, idx = [], []
    logc = np.log(closes)
    rets = np.diff(logc, prepend=logc[0])

    for t in range(126, n):
        window = closes[: t + 1]
        r = rets[: t + 1]
        ma50 = window[-50:].mean()
        vol21 = r[-21:].std(ddof=1) or 1e-6
        vol63 = r[-63:].std(ddof=1) or 1e-6

        gains = np.clip(r[-3:], 0, None).sum()
        losses = -np.clip(r[-3:], None, 0).sum()
        rsi2 = 100.0 * gains / (gains + losses) if (gains + losses) > 1e-12 else 50.0

        rows.append([
            float(logc[t] - logc[t - 21]),
            float(logc[t] - logc[t - 63]),
            float(logc[t] - logc[t - 126]),
            float((logc[t] - logc[t - 5]) / (vol21 * math.sqrt(5))),
            float(vol21 / vol63),
            float((window[-1] - ma50) / ma50),
            float(rsi2 / 100.0),
            float(1.0 if window[-1] > ma50 else 0.0),
        ])
        idx.append(t)
    return np.asarray(rows), idx


def _standardize(train: np.ndarray, other: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = train.mean(axis=0)
    sd = train.std(axis=0)
    sd[sd < 1e-9] = 1.0
    return (train - mu) / sd, (other - mu) / sd


def _fit_logistic(X: np.ndarray, y: np.ndarray, *, l2: float = 1.0,
                  iters: int = 400, lr: float = 0.1) -> np.ndarray:
    """
    Regularized logistic regression by gradient descent.

    L2 is deliberately strong. With eight features, a few hundred observations
    and a signal-to-noise ratio this poor, an unregularized fit will happily
    memorize the training window and report a beautiful in-sample number that
    evaporates out of sample.
    """
    A = np.column_stack([np.ones(X.shape[0]), X])
    w = np.zeros(A.shape[1])
    for _ in range(iters):
        z = A @ w
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        grad = A.T @ (p - y) / A.shape[0]
        grad[1:] += l2 * w[1:] / A.shape[0]
        w -= lr * grad
    return w


def _predict_logistic(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    A = np.column_stack([np.ones(X.shape[0]), X])
    return 1.0 / (1.0 + np.exp(-np.clip(A @ w, -30, 30)))


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    pos, neg = p[y == 1], p[y == 0]
    if pos.size == 0 or neg.size == 0:
        return 0.5
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(order.size)
    ranks[order] = np.arange(1, order.size + 1)
    return float((ranks[: pos.size].sum() - pos.size * (pos.size + 1) / 2)
                 / (pos.size * neg.size))


@dataclass
class DirectionalResult:
    symbol: str
    horizon_days: int
    probability_up: float
    base_rate: float
    edge_vs_base_rate: float
    out_of_sample: dict
    folds: list[dict]
    feature_weights: dict
    verdict: str
    trustworthy: bool
    observations: int

    def to_dict(self) -> dict:
        return asdict(self)


def directional_forecast(closes: Sequence[float], symbol: str = "",
                         horizon_days: int = 21, n_folds: int = 5) -> DirectionalResult | None:
    """
    Probability the underlying is higher in `horizon_days`, with walk-forward
    validation.

    Every reported score is out-of-sample. The model is refit on each expanding
    training window and scored only on the block that follows it, so nothing it
    reports has seen its own answer. The comparison that matters is against the
    base rate: equities drift up, so a model that always says "up" scores well
    above 50% while knowing nothing. `edge_vs_base_rate` is the honest number.
    """
    c = np.asarray(closes, dtype=float)
    c = c[c > 0]
    if c.size < 320:
        return None

    X_all, idx = _build_features(c)
    logc = np.log(c)

    keep, y_list = [], []
    for row_i, t in enumerate(idx):
        if t + horizon_days >= c.size:
            continue
        keep.append(row_i)
        y_list.append(1.0 if logc[t + horizon_days] > logc[t] else 0.0)
    if len(keep) < 150:
        return None

    X = X_all[keep]
    y = np.asarray(y_list)

    # Walk-forward: expanding train, contiguous test block after it. Blocks are
    # separated by the horizon so a test label cannot overlap a training one.
    fold_size = len(y) // (n_folds + 1)
    folds, probs_oos, ys_oos = [], [], []
    for k in range(1, n_folds + 1):
        tr_end = fold_size * k
        te_start = tr_end + horizon_days          # embargo
        te_end = min(te_start + fold_size, len(y))
        if te_end - te_start < 20 or tr_end < 60:
            continue
        Xtr, Xte = _standardize(X[:tr_end], X[te_start:te_end])
        w = _fit_logistic(Xtr, y[:tr_end])
        p = _predict_logistic(w, Xte)
        yte = y[te_start:te_end]

        acc = float(((p > 0.5) == (yte == 1)).mean())
        base = float(max(yte.mean(), 1 - yte.mean()))
        brier = float(np.mean((p - yte) ** 2))
        folds.append({
            "fold": k, "train_size": tr_end, "test_size": int(te_end - te_start),
            "accuracy": round(acc, 4), "base_rate": round(base, 4),
            "edge": round(acc - base, 4), "auc": round(_auc(yte, p), 4),
            "brier": round(brier, 4),
        })
        probs_oos.append(p)
        ys_oos.append(yte)

    if not folds:
        return None

    p_all = np.concatenate(probs_oos)
    y_all = np.concatenate(ys_oos)
    acc = float(((p_all > 0.5) == (y_all == 1)).mean())
    base = float(max(y_all.mean(), 1 - y_all.mean()))
    auc = _auc(y_all, p_all)
    brier = float(np.mean((p_all - y_all) ** 2))
    brier_base = float(np.mean((y_all.mean() - y_all) ** 2))
    edge = acc - base

    Xtr_full, Xnow = _standardize(X, X[-1:].copy())
    w_full = _fit_logistic(Xtr_full, y)
    p_now = float(_predict_logistic(w_full, Xnow)[0])

    # A binomial standard error on the out-of-sample accuracy: with a few
    # hundred observations, anything under ~3 points of edge is noise.
    se = math.sqrt(0.25 / len(y_all))
    significant = edge > 2 * se and auc > 0.55 and brier < brier_base

    if significant:
        verdict = (f"Out-of-sample accuracy {acc:.1%} against a {base:.1%} base rate "
                   f"({edge:+.1%}), AUC {auc:.3f}. That is a measurable edge on this "
                   f"history, but it is small and could still be a survivor of the "
                   f"model choices made here. Size accordingly.")
    else:
        verdict = (f"Out-of-sample accuracy {acc:.1%} against a {base:.1%} base rate "
                   f"({edge:+.1%}), AUC {auc:.3f}, Brier {brier:.4f} versus {brier_base:.4f} "
                   f"for always predicting the base rate. This model has no demonstrable "
                   f"directional edge. That is the expected result — short-horizon equity "
                   f"direction is close to unforecastable — and the probability below "
                   f"should be read as roughly the base rate, not as a signal.")

    return DirectionalResult(
        symbol=symbol.upper(), horizon_days=horizon_days,
        probability_up=p_now, base_rate=base, edge_vs_base_rate=edge,
        out_of_sample={
            "accuracy": round(acc, 4), "base_rate": round(base, 4),
            "edge": round(edge, 4), "auc": round(auc, 4),
            "brier": round(brier, 5), "brier_base_rate": round(brier_base, 5),
            "standard_error": round(se, 4),
            "statistically_meaningful": bool(significant),
            "samples": int(len(y_all)),
        },
        folds=folds,
        feature_weights={f: round(float(w_full[i + 1]), 4) for i, f in enumerate(FEATURES)},
        verdict=verdict,
        trustworthy=bool(significant),
        observations=int(len(y)),
    )


# --------------------------------------------------------------------------
# Cross-asset: beta decomposition
# --------------------------------------------------------------------------

@dataclass
class BetaDecomposition:
    symbol: str
    benchmark: str
    beta: float
    alpha_annual: float
    r_squared: float
    correlation: float
    total_vol: float
    systematic_vol: float
    idiosyncratic_vol: float
    observations: int

    def to_dict(self) -> dict:
        return asdict(self)


def beta_decompose(stock_closes: Sequence[float], bench_closes: Sequence[float],
                   symbol: str = "", benchmark: str = "SPY") -> BetaDecomposition | None:
    """
    Split a name's volatility into the part the index explains and the part it
    does not.

    This is what makes a sector or index view actionable at the single-stock
    level: forecasting the index tells you about the systematic component only,
    and for a high-idiosyncratic name that is a minority of the risk. Reporting
    the split stops an index call from being mistaken for a stock call.
    """
    rs = log_returns(stock_closes)
    rb = log_returns(bench_closes)
    n = min(rs.size, rb.size)
    if n < 60:
        return None
    rs, rb = rs[-n:], rb[-n:]

    var_b = float(np.var(rb, ddof=1))
    if var_b <= 1e-12:
        return None
    beta = float(np.cov(rs, rb, ddof=1)[0, 1] / var_b)
    alpha_daily = float(rs.mean() - beta * rb.mean())
    resid = rs - (alpha_daily + beta * rb)

    total_vol = float(rs.std(ddof=1) * math.sqrt(TRADING_DAYS))
    idio_vol = float(resid.std(ddof=1) * math.sqrt(TRADING_DAYS))
    syst_vol = float(abs(beta) * rb.std(ddof=1) * math.sqrt(TRADING_DAYS))
    corr = float(np.corrcoef(rs, rb)[0, 1])

    return BetaDecomposition(
        symbol=symbol.upper(), benchmark=benchmark.upper(),
        beta=beta, alpha_annual=alpha_daily * TRADING_DAYS,
        r_squared=corr * corr, correlation=corr,
        total_vol=total_vol, systematic_vol=syst_vol,
        idiosyncratic_vol=idio_vol, observations=n,
    )


def propagate_move(beta: BetaDecomposition, benchmark_move_pct: float) -> dict:
    """
    Translate an index move into an expected stock move, with the uncertainty
    the idiosyncratic component implies.

    The point estimate is beta times the index move. The band around it is the
    idiosyncratic volatility over the same horizon -- and on most single names
    that band is wider than the point estimate, which is precisely why "the
    index will rally" is not a stock thesis.
    """
    expected = beta.beta * benchmark_move_pct
    # One-day-equivalent idiosyncratic uncertainty scaled to the size of the
    # move being propagated.
    horizon_days = max((benchmark_move_pct / (0.01)) ** 2, 1.0)
    idio_sd = beta.idiosyncratic_vol * math.sqrt(min(horizon_days, 252) / TRADING_DAYS)
    return {
        "benchmark_move": benchmark_move_pct,
        "expected_stock_move": expected,
        "idiosyncratic_sd": idio_sd,
        "range_68": [expected - idio_sd, expected + idio_sd],
        "range_95": [expected - 2 * idio_sd, expected + 2 * idio_sd],
        "explained_fraction": beta.r_squared,
        "note": (f"The index explains {beta.r_squared:.0%} of this name's variance. "
                 f"The remaining {1 - beta.r_squared:.0%} is idiosyncratic and no "
                 f"index view speaks to it."),
    }
