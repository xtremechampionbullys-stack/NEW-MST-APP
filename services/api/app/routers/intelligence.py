"""Forecasting, forward testing, and the dojo."""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..db.store import get_store
from ..engine.backtest import BacktestRules
from ..engine.dojo import SearchSpace, run_dojo
from ..engine.forecast import (
    beta_decompose,
    directional_forecast,
    fit_garch,
    har_rv_forecast,
    project_vol_to_dates,
    propagate_move,
)
from ..engine.forward import (
    ManagementRules,
    SimulationConfig,
    calibration_report,
    forward_test,
    score_paper_trade,
)
from ..providers.base import ProviderError
from ..providers.registry import get_registry
from ..services import earnings_service
from .strategy import AnalyzeRequest, _build_context, _position_from_legs, _sanitize

log = logging.getLogger(__name__)

forecast_router = APIRouter(prefix="/api/forecast", tags=["forecast"])
forward_router = APIRouter(prefix="/api/forward", tags=["forward"])
dojo_router = APIRouter(prefix="/api/dojo", tags=["dojo"])


# ==========================================================================
# Forecast
# ==========================================================================

@forecast_router.get("/volatility/{symbol}")
async def volatility_forecast(symbol: str,
                              horizons: str = Query("7,30,60,90",
                                                    description="Comma separated days ahead"),
                              lookback: int = Query(900, ge=250, le=5000)):
    """
    Projected volatility to specific dates.

    Shape from GARCH mean reversion, level from HAR-RV where its out-of-sample
    fit earns it, plus additive variance for any scheduled event inside the
    window. Every response carries the out-of-sample R-squared: volatility is
    forecastable, but not equally on every name, and the number that says so
    ships with the number it qualifies.
    """
    registry = get_registry()
    try:
        bars = await registry.bars(symbol, limit=lookback)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    if len(bars) < 250:
        raise HTTPException(422, f"only {len(bars)} bars available; need at least 250")

    closes = [b.c for b in bars]
    today = dt.date.today()
    try:
        days = [int(d) for d in horizons.split(",") if d.strip()]
    except ValueError as exc:
        raise HTTPException(422, "horizons must be comma separated integers") from exc
    targets = [today + dt.timedelta(days=d) for d in days if d > 0]

    implied_now = None
    try:
        from ..services.vol_service import atm_iv_from_chain
        chain = await registry.chain(symbol, max_expirations=6)
        implied_now = atm_iv_from_chain(chain, 30)
    except ProviderError:
        pass

    event_dates: list[dt.date] = []
    try:
        events, _ = await earnings_service.load_events(symbol)
        event_dates = [e.event_date for e in events if e.event_date >= today]
    except Exception:                                          # noqa: BLE001
        pass

    projection = project_vol_to_dates(closes, targets, today=today,
                                      implied_now=implied_now,
                                      event_dates=event_dates)
    har = har_rv_forecast(closes, horizon_days=5, bars=bars)
    return _sanitize({
        "symbol": symbol.upper(),
        "bars": len(bars),
        "implied_now": implied_now,
        "har_short_horizon": har.to_dict() if har else None,
        **projection,
    })


@forecast_router.get("/direction/{symbol}")
async def direction_forecast(symbol: str,
                             horizon_days: int = Query(21, ge=1, le=252),
                             lookback: int = Query(1200, ge=400, le=5000)):
    """
    Probability the underlying is higher in `horizon_days`.

    Read `out_of_sample` before `probability_up`. Equities drift upward, so a
    model that always says "up" already scores well above 50% — `edge` is the
    figure that says whether this one knows anything the base rate does not.
    Where it does not, `verdict` says so plainly and the probability should be
    read as roughly the base rate.
    """
    try:
        bars = await get_registry().bars(symbol, limit=lookback)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    result = directional_forecast([b.c for b in bars], symbol, horizon_days)
    if result is None:
        raise HTTPException(422,
                            f"need at least 320 bars for a validated directional "
                            f"model; got {len(bars)}")
    return _sanitize(result.to_dict())


@forecast_router.get("/cross-asset/{symbol}")
async def cross_asset(symbol: str, benchmark: str = "SPY",
                      sector_proxy: str | None = None,
                      benchmark_move: float = Query(0.05, ge=-0.5, le=0.5),
                      lookback: int = Query(500, ge=120, le=5000)):
    """
    How much of this name is the index, and how much is not.

    A sector or index view only speaks to the systematic component. On a
    high-idiosyncratic name that is a minority of the risk, so the response
    reports the split and puts a band around the propagated move rather than a
    point estimate.
    """
    registry = get_registry()
    try:
        stock = await registry.bars(symbol, limit=lookback)
        bench = await registry.bars(benchmark, limit=lookback)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    decomp = beta_decompose([b.c for b in stock], [b.c for b in bench],
                            symbol, benchmark)
    if decomp is None:
        raise HTTPException(422, "not enough overlapping history")

    payload = {
        "benchmark": decomp.to_dict(),
        "propagation": propagate_move(decomp, benchmark_move),
    }

    if sector_proxy:
        try:
            sec = await registry.bars(sector_proxy, limit=lookback)
            sd = beta_decompose([b.c for b in stock], [b.c for b in sec],
                                symbol, sector_proxy)
            if sd:
                payload["sector"] = {
                    "decomposition": sd.to_dict(),
                    "propagation": propagate_move(sd, benchmark_move),
                }
        except ProviderError as exc:
            payload["sector_error"] = str(exc)

    return _sanitize(payload)


@forecast_router.get("/index/{symbol}")
async def index_forecast(symbol: str = "SPY",
                         horizon_days: int = Query(21, ge=1, le=252),
                         lookback: int = Query(1200, ge=400, le=5000)):
    """Volatility and direction for an index or sector proxy, in one call."""
    try:
        bars = await get_registry().bars(symbol, limit=lookback)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    closes = [b.c for b in bars]
    har = har_rv_forecast(closes, horizon_days=5, bars=bars)
    garch = fit_garch(closes)
    direction = directional_forecast(closes, symbol, horizon_days)

    return _sanitize({
        "symbol": symbol.upper(),
        "volatility": {
            "har": har.to_dict() if har else None,
            "garch": garch.to_dict() if garch else None,
            "term_structure": garch.project(min(horizon_days * 3, 180)) if garch else [],
        },
        "direction": direction.to_dict() if direction else None,
    })


# ==========================================================================
# Forward test
# ==========================================================================

class ForwardRequest(AnalyzeRequest):
    paths: int = Field(4000, ge=200, le=20000)
    horizon_days: int | None = None
    annual_drift: float = Field(0.0, description="0 = risk-neutral")
    tail_df: float = Field(4.0, ge=2.5, le=50,
                           description="Student-t degrees of freedom; higher = thinner tails")
    profit_target_pct: float | None = 0.50
    stop_loss_pct: float | None = 2.00
    close_at_dte: int | None = 21
    use_forecast_vol: bool = Field(
        True, description="Calibrate the simulation to the HAR/GARCH forecast "
                          "rather than the chain's implied volatility.")


@forward_router.post("/simulate")
async def simulate(req: ForwardRequest):
    """
    Monte Carlo the position forward, applying management rules along each path.

    This answers what the closed forms cannot: whether the stop is hit before
    the target, how far a winning trade goes underwater first, and how long
    capital stays committed.
    """
    ctx, meta = await _build_context(req)

    if req.template:
        from ..engine.strategies import get_template
        try:
            position = get_template(req.template).make(ctx, req.params)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
    else:
        position = _position_from_legs(req, ctx)

    base_vol = position.blended_iv
    vol_source = "chain implied"
    if req.use_forecast_vol:
        try:
            bars = await get_registry().bars(req.symbol, limit=900)
            har = har_rv_forecast([b.c for b in bars], horizon_days=5, bars=bars)
            # Only trust the forecast where it has demonstrated out-of-sample
            # skill on this name. Otherwise implied volatility is the better
            # estimate and we say which one was used.
            if har and (har.out_of_sample_r2 or 0) > 0.05:
                base_vol = har.forecast_vol
                vol_source = f"HAR forecast (out-of-sample R2 {har.out_of_sample_r2:.2f})"
            else:
                vol_source = ("chain implied — the volatility model showed no "
                              "out-of-sample skill on this name")
        except ProviderError:
            pass

    event_days: list[int] = []
    jump_vol = 0.0
    try:
        events = await earnings_service.events_in_window(req.symbol, position.max_dte)
        event_days = [e.days_away for e in events if e.days_away > 0]
        if event_days:
            report = await earnings_service.earnings_report(req.symbol)
            move = (report.get("implied_move") or {})
            block = (move.get("term_structure") or move.get("straddle") or {})
            ev = block.get("event_vol")
            jump_vol = float(ev) if ev else 0.06
    except Exception:                                          # noqa: BLE001
        pass

    result = forward_test(
        position,
        config=SimulationConfig(paths=req.paths, horizon_days=req.horizon_days,
                                annual_drift=req.annual_drift, tail_df=req.tail_df),
        rules=ManagementRules(profit_target_pct=req.profit_target_pct,
                              stop_loss_pct=req.stop_loss_pct,
                              close_at_dte=req.close_at_dte),
        base_vol=base_vol, event_days=event_days, event_jump_vol=jump_vol,
    )
    result["symbol"] = req.symbol.upper()
    result["template"] = req.template
    result["meta"] = meta
    result["volatility_source"] = vol_source
    result["events_in_window"] = event_days
    return _sanitize(result)


class PaperTradeRequest(BaseModel):
    symbol: str
    name: str = "Paper trade"
    template_id: str | None = None
    legs: list[dict]
    expectation: dict = Field(
        ..., description="What the engine predicted at entry. Frozen, never recomputed.")
    notes: str = ""


@forward_router.post("/paper")
async def open_paper_trade(req: PaperTradeRequest):
    """
    Record a simulated trade together with what the engine predicted at entry.

    The expectation is stored verbatim and never recomputed. A prediction that
    gets quietly revised as evidence arrives is not a prediction, and the entire
    point of this log is to eventually falsify the probability engine if it
    deserves it.
    """
    if req.expectation.get("probability_of_profit") is None:
        raise HTTPException(
            422, "a paper trade must record a probability_of_profit at entry — "
                 "without it the trade can never contribute to calibration, "
                 "which is the only reason to log it")

    row = await get_store().add_position(
        symbol=req.symbol, name=req.name, legs=req.legs,
        notes=req.notes, broker="paper",
        entry_debit=req.expectation.get("entry_basis"),
        expectation=req.expectation)
    await get_store().record_mark(row["id"], underlying=None,
                                  mark_value=None, unrealized=0.0)
    return _sanitize(row)


@forward_router.get("/calibration")
async def calibration(scored: str | None = None):
    """
    Are the predicted probabilities honest?

    Buckets closed paper trades by predicted win probability and compares each
    bucket's predicted rate to its realized rate. If trades entered at a 70%
    predicted win rate win about 70% of the time, the engine is calibrated —
    whatever any individual trade did.
    """
    rows = await get_store().list_positions(open_only=False)
    closed = [r for r in rows if r.get("closed") and r.get("broker") == "paper"]

    scored_rows, unscorable = [], 0
    for r in closed:
        exp = r.get("expectation")
        if not isinstance(exp, dict) or exp.get("probability_of_profit") is None:
            unscorable += 1
            continue
        pl = (r.get("exit_credit") or 0.0) - (r.get("entry_debit") or 0.0)
        scored_rows.append(score_paper_trade(exp, pl, _days_held(r)))

    return _sanitize({
        "closed_paper_trades": len(closed),
        "scored": len(scored_rows),
        # Trades logged before the expectation was recorded can never be
        # scored. Counting them separately keeps the denominator honest — a
        # calibration report over 12 of 40 trades is not a report over 40.
        "unscorable": unscorable,
        "calibration": calibration_report(scored_rows),
    })


def _days_held(row: dict) -> int:
    try:
        opened = dt.date.fromisoformat(row["opened"])
        closed = dt.date.fromisoformat(row["closed"])
    except (KeyError, TypeError, ValueError):
        return 0
    return (closed - opened).days


# ==========================================================================
# Dojo
# ==========================================================================

class DojoRequest(BaseModel):
    symbol: str = "SPY"
    template_id: str = "short_strangle"
    params: dict = Field(default_factory=dict)
    variants: int = Field(40, ge=5, le=200)
    train_fraction: float = Field(0.6, ge=0.3, le=0.8)
    lookback_days: int = Field(1200, ge=400, le=5000)
    skip_earnings: bool = False
    seed: int = 42


@dojo_router.post("/run")
async def dojo_run(req: DojoRequest):
    """
    Search the rule space, select on a training window, score out of sample.

    Read `validation.out_of_sample_percentile` first. It answers the only
    question that matters about an optimization: did picking the in-sample
    winner beat picking at random, measured on data the selection never saw? If
    it did not, the leaderboard below it is noise, and the verdict will say so.
    """
    registry = get_registry()
    try:
        bars = await registry.bars(req.symbol, limit=req.lookback_days)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    earnings_dates: list[dt.date] = []
    if req.skip_earnings:
        try:
            events, _ = await earnings_service.load_events(
                req.symbol, lookback_quarters=20, lookahead_days=30)
            earnings_dates = [e.event_date for e in events]
        except Exception:                                      # noqa: BLE001
            pass

    base = BacktestRules(template_id=req.template_id, params=req.params,
                         skip_earnings=req.skip_earnings)
    try:
        result = run_dojo(bars, base, variants=req.variants,
                          train_fraction=req.train_fraction,
                          symbol=req.symbol, seed=req.seed,
                          earnings_dates=earnings_dates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, f"unknown template: {exc}") from exc

    return _sanitize(result)


@dojo_router.get("/search-space")
async def search_space():
    space = SearchSpace()
    return {"space": space.to_dict(), "combinations": space.size()}
