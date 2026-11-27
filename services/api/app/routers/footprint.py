"""
Order-flow footprint API: the chart, the POC-defence strategy, its backtest.

Everything here runs on a *trade tape* -- individual prints with an aggressor
side -- because that is the only input a footprint can honestly be built from.
One-minute OHLCV bars cannot be turned into bid/ask volume by price row; any
tool that appears to do so is inventing the inside of the candle.

Where the tape comes from, in order of how much a result on it means:

1. An **uploaded tape** (POST /upload): real trades from an exchange or
   platform export. Aggressor-flagged data -- futures, crypto -- is exact.
2. The **live provider**, where the plan includes trade-level data. For US
   equities the side is inferred by the tick rule and labelled as such.
3. The **synthetic tape** (demo mode only): generated, with no edge built in.
   It exists to show the chart and prove the machinery; every response built
   on it says so.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import fields

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..engine.footprint import (
    StrategyParams,
    SweepGrid,
    TradeTape,
    build_footprint,
    find_signals,
    parse_trades_csv,
    run_backtest,
    run_sweep,
    suggest_row_size,
)
from ..providers.base import ProviderError
from ..providers.registry import get_registry

router = APIRouter(prefix="/api/footprint", tags=["footprint"])

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_UPLOAD_TRADES = 3_000_000
_UPLOADS: OrderedDict[str, tuple[TradeTape, float]] = OrderedDict()
_UPLOAD_SLOTS = 4


class ParamsModel(BaseModel):
    """Strategy parameters. Anything omitted keeps the engine default."""
    volume_mult: float | None = Field(default=None, ge=1.0, le=10.0)
    volume_lookback: int | None = Field(default=None, ge=5, le=200)
    poc_zone: float | None = Field(default=None, ge=0.05, le=0.5)
    require_delta: bool | None = None
    min_stack: int | None = Field(default=None, ge=0, le=10)
    trend_filter: bool | None = None
    allow_long: bool | None = None
    allow_short: bool | None = None
    min_range_ticks: float | None = Field(default=None, ge=0, le=1000)
    entry_ttl_bars: int | None = Field(default=None, ge=1, le=30)
    fill_through: bool | None = None
    stop_buffer_ticks: float | None = Field(default=None, ge=0, le=500)
    min_risk_ticks: float | None = Field(default=None, ge=1, le=1000)
    target1_r: float | None = Field(default=None, gt=0, le=20)
    target2_r: float | None = Field(default=None, gt=0, le=50)
    scale_out: float | None = Field(default=None, ge=0, lt=1)
    breakeven_after_t1: bool | None = None
    max_hold_bars: int | None = Field(default=None, ge=1, le=600)
    flatten_at_session_end: bool | None = None
    tick_size: float | None = Field(default=None, gt=0)
    slippage_ticks: float | None = Field(default=None, ge=0, le=50)
    commission_per_unit: float | None = Field(default=None, ge=0)
    point_value: float | None = Field(default=None, gt=0)
    starting_equity: float | None = Field(default=None, gt=0)
    risk_per_trade_pct: float | None = Field(default=None, gt=0, le=10)
    max_leverage: float | None = Field(default=None, gt=0, le=100)


class RunRequest(BaseModel):
    symbol: str = Field(default="SPY", min_length=1, max_length=20)
    tape_id: str | None = Field(default=None, max_length=64,
                                description="Use an uploaded tape instead of the provider")
    sessions: int = Field(default=3, ge=1, le=10)
    row_size: float | None = Field(default=None, gt=0,
                                   description="Footprint row height; auto when omitted")
    params: ParamsModel = Field(default_factory=ParamsModel)
    control_runs: int = Field(default=200, ge=0, le=1000)
    window: int = Field(default=30, ge=5, le=120,
                        description="Bars returned with full row detail for the chart")
    window_end: int | None = Field(default=None, ge=1,
                                   description="Exclusive bar index the chart window ends at")


class SweepRequest(BaseModel):
    symbol: str = Field(default="SPY", min_length=1, max_length=20)
    tape_id: str | None = Field(default=None, max_length=64)
    sessions: int = Field(default=5, ge=2, le=10)
    row_size: float | None = Field(default=None, gt=0)
    params: ParamsModel = Field(default_factory=ParamsModel)
    train_fraction: float = Field(default=0.6, ge=0.3, le=0.8)
    min_trades: int = Field(default=20, ge=5, le=500)


class UploadRequest(BaseModel):
    csv: str = Field(..., min_length=10, description="Trade tape as CSV text")
    tick_size: float | None = Field(default=None, gt=0)
    label: str = Field(default="upload", max_length=80)


def infer_tick_size(tape: TradeTape) -> float:
    """Smallest price step actually present in the tape, snapped to a sane grid."""
    sample = np.unique(np.round(tape.price[: 200_000], 8))
    if sample.size < 2:
        return 0.01
    step = float(np.min(np.diff(sample)))
    for cand in (1e-4, 5e-4, 1e-3, 5e-3, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 5, 10):
        if step <= cand * 1.0001:
            return cand
    return step


async def _tape_for(symbol: str, tape_id: str | None, sessions: int
                    ) -> tuple[TradeTape, float | None]:
    if tape_id:
        hit = _UPLOADS.get(tape_id)
        if hit is None:
            raise HTTPException(404, "that uploaded tape is no longer held (uploads "
                                     "live in memory and the oldest are dropped); "
                                     "upload it again")
        _UPLOADS.move_to_end(tape_id)
        return hit
    try:
        tape = await get_registry().trades(symbol.upper(), sessions=sessions)
    except ProviderError as exc:
        raise HTTPException(
            503 if exc.provider == "configuration" else 502,
            f"No tick-level trade data is available for {symbol.upper()}: {exc}. "
            "A footprint cannot be built from bars. Upload a trade tape "
            "(POST /api/footprint/upload) or use a data plan that includes "
            "trades.") from exc
    return tape, None


def _resolve(req_params: ParamsModel, tape: TradeTape, tick: float | None,
             row_size: float | None) -> tuple[StrategyParams, float]:
    given = req_params.model_dump(exclude_none=True)
    if "tick_size" not in given:
        given["tick_size"] = tick or infer_tick_size(tape)
    p = StrategyParams(**given)
    if p.scale_out > 0 and p.target2_r <= p.target1_r:
        raise HTTPException(422, "target2_r must be greater than target1_r when "
                                 "scaling out")
    rs = row_size or suggest_row_size(tape, p.tick_size)
    return p, rs


def _meta(symbol: str, tape: TradeTape, p: StrategyParams, row_size: float,
          n_bars: int) -> dict:
    synthetic = tape.classified == "generated"
    return {
        "symbol": symbol.upper(),
        "source": tape.source,
        "synthetic": synthetic,
        "aggressor": tape.classified,
        "trades": len(tape),
        "bars": n_bars,
        "row_size": row_size,
        "tick_size": p.tick_size,
        "from": float(tape.ts[0]),
        "to": float(tape.ts[-1]),
        "note": (
            "SYNTHETIC tape: generated trades with no edge built in. The chart "
            "and the mechanics are real; the numbers say nothing about any "
            "market." if synthetic else
            "Aggressor side inferred by the tick rule (the feed carries no "
            "flag); delta and imbalance are estimates."
            if tape.classified == "tick_rule" else
            "Aggressor side as reported by the venue."),
    }


@router.get("/defaults")
async def defaults():
    """The strategy's parameters with their defaults, for building a form."""
    p = StrategyParams()
    return {"params": p.to_dict(),
            "fields": [f.name for f in fields(StrategyParams)],
            "sweep_grid": SweepGrid().__dict__}


@router.post("/upload")
async def upload(req: UploadRequest):
    """
    Hold a real trade tape in memory and return an id for /run and /sweep.

    CSV with a header naming time, price, size and optionally side; or a
    headerless Binance aggTrades file. Without a side column the aggressor is
    inferred by the tick rule.
    """
    if len(req.csv.encode()) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "tape too large; trim it to a few sessions")
    try:
        tape = parse_trades_csv(req.csv, source=req.label or "upload")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(tape) > MAX_UPLOAD_TRADES:
        raise HTTPException(413, f"{len(tape):,} trades exceeds the "
                                 f"{MAX_UPLOAD_TRADES:,} limit; trim the tape")
    tick = req.tick_size or infer_tick_size(tape)
    tape_id = hashlib.sha256(req.csv[:4096].encode() + str(len(tape)).encode()).hexdigest()[:16]
    _UPLOADS[tape_id] = (tape, tick)
    _UPLOADS.move_to_end(tape_id)
    while len(_UPLOADS) > _UPLOAD_SLOTS:
        _UPLOADS.popitem(last=False)
    return {"tape_id": tape_id, "trades": len(tape), "aggressor": tape.classified,
            "tick_size": tick, "from": float(tape.ts[0]), "to": float(tape.ts[-1]),
            "label": tape.source}


@router.post("/run")
async def run(req: RunRequest):
    """
    Build the footprint, find signals, backtest them, and return a chart window.

    `bars` carries OHLC, delta and POC for every bar (light enough for the
    whole tape); `window.bars` carries the full bid x ask rows for the bars the
    chart draws.
    """
    tape, tick = await _tape_for(req.symbol, req.tape_id, req.sessions)
    p, row_size = _resolve(req.params, tape, tick, req.row_size)
    bars = build_footprint(tape, row_size=row_size)
    if len(bars) < p.volume_lookback + 5:
        raise HTTPException(422, f"only {len(bars)} one-minute bars in the tape; "
                                 "need more history than the volume lookback")
    result = run_backtest(bars, tape, p, control_runs=req.control_runs)

    end = min(req.window_end or len(bars), len(bars))
    start = max(0, end - req.window)
    return {
        "meta": _meta(req.symbol, tape, p, row_size, len(bars)),
        "bars": [b.to_dict(with_rows=False) for b in bars],
        "window": {"start": start, "end": end,
                   "bars": [b.to_dict() for b in bars[start:end]]},
        **result,
    }


@router.get("/bars")
async def bars_only(symbol: str = "SPY", sessions: int = 1, window: int = 30,
                    row_size: float | None = None, end: int | None = None,
                    tape_id: str | None = None):
    """
    Footprint bars with full row detail, no backtest: the chart's pager.

    `end` is the exclusive bar index the window stops at (default: the latest
    bar), so the chart can step back through a tape without re-running the
    backtest. Signals are those of the default parameters.
    """
    if not (1 <= sessions <= 10 and 5 <= window <= 120):
        raise HTTPException(422, "sessions must be 1-10 and window 5-120")
    if row_size is not None and row_size <= 0:
        raise HTTPException(422, "row_size must be positive")
    tape, tick = await _tape_for(symbol, tape_id, sessions)
    p, rs = _resolve(ParamsModel(), tape, tick, row_size)
    bars = build_footprint(tape, row_size=rs)
    stop = min(max(end or len(bars), 1), len(bars))
    start = max(0, stop - window)
    return {"meta": _meta(symbol, tape, p, rs, len(bars)),
            "bars": [b.to_dict() for b in bars[start:stop]],
            "signals": [s.to_dict() for s in find_signals(bars, p)
                        if start <= s.bar < stop],
            "start": start, "end": stop}


@router.post("/sweep")
async def sweep(req: SweepRequest):
    """Walk-forward parameter sweep: choose on the first part, score on the rest."""
    tape, tick = await _tape_for(req.symbol, req.tape_id, req.sessions)
    p, row_size = _resolve(req.params, tape, tick, req.row_size)
    bars = build_footprint(tape, row_size=row_size)
    if len(bars) < 200:
        raise HTTPException(422, f"{len(bars)} bars is too few to split into "
                                 "training and test windows")
    out = run_sweep(bars, tape, p, train_fraction=req.train_fraction,
                    min_trades=req.min_trades)
    out["meta"] = _meta(req.symbol, tape, p, row_size, len(bars))
    return out
