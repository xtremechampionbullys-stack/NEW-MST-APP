"""Backtest endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..db.store import get_store, store_status
from ..engine.backtest import BacktestRules
from ..providers.base import ProviderError
from ..services import backtest_service

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class RulesInput(BaseModel):
    template_id: str = "short_strangle"
    params: dict = Field(default_factory=dict)

    entry_every_days: int = Field(7, ge=1, le=90)
    target_dte: int = Field(45, ge=1, le=720)
    contracts: int = Field(1, ge=1, le=100)
    max_concurrent: int = Field(1, ge=1, le=20)

    profit_target_pct: float | None = Field(
        0.50, description="Fraction of the entry cost basis. Null disables.")
    stop_loss_pct: float | None = Field(
        2.00, description="Multiple of the entry cost basis. Null disables.")
    close_at_dte: int | None = Field(21, description="Null disables.")
    skip_earnings: bool = False

    commission_per_contract: float = Field(0.65, ge=0, le=10)
    slippage_pct: float = Field(0.01, ge=0, le=0.25)
    variance_risk_premium: float = Field(
        1.15, ge=0.5, le=3.0,
        description="Implied/realized ratio used when prices are modelled. This "
                    "is the single most consequential assumption in a modelled "
                    "backtest — raise it and every short-premium result improves.")

    def to_rules(self) -> BacktestRules:
        return BacktestRules(**self.model_dump())


class RunRequest(BaseModel):
    symbol: str = "SPY"
    rules: RulesInput = Field(default_factory=RulesInput)
    lookback_days: int = Field(730, ge=200, le=5000)
    persist: bool = True
    label: str = ""


@router.post("/run")
async def run_backtest_endpoint(req: RunRequest):
    """
    Replay a rule set over history.

    The response always carries `quote_source`, `is_historical_pricing` and a
    `caveat`. Read them before reading the metrics: a modelled run tests the
    rules against real price paths but cannot tell you what you would have been
    filled at.
    """
    try:
        return await backtest_service.run(
            req.symbol, req.rules.to_rules(),
            lookback_days=req.lookback_days, persist=req.persist,
            label=req.label)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc


class CompareRequest(BaseModel):
    symbol: str = "SPY"
    rule_sets: list[RulesInput]
    lookback_days: int = Field(730, ge=200, le=5000)


@router.post("/compare")
async def compare_backtests(req: CompareRequest):
    """Run several rule sets over identical history."""
    if not req.rule_sets:
        raise HTTPException(422, "supply at least one rule set")
    try:
        return await backtest_service.compare(
            req.symbol, [r.to_rules() for r in req.rule_sets],
            lookback_days=req.lookback_days)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/runs")
async def list_runs(symbol: str | None = None,
                    limit: int = Query(50, ge=1, le=200)):
    rows = await get_store().list_backtests(symbol, limit)
    return {"count": len(rows), "backend": get_store().backend,
            "persistent": store_status()["persistent"], "runs": rows}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    row = await get_store().get_backtest(run_id)
    if not row:
        raise HTTPException(404, "backtest run not found")
    return row


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    if not await get_store().delete_backtest(run_id):
        raise HTTPException(404, "backtest run not found")
    return {"deleted": run_id}
