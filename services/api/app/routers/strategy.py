"""Strategy tester endpoints: templates, analysis, payoff curves, guidance."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..ai.guidance import evaluate, suggest_strategies
from ..config import get_settings
from ..engine.analytics import analyze
from ..engine.payoff import Leg, Position
from ..engine.strategies import (
    MarketContext,
    categories,
    get_template,
    list_templates,
)
from ..engine.vol import ChainVolSurface, ParametricVolSurface
from ..providers.base import OptionChain, ProviderError
from ..providers.registry import get_registry
from ..services import earnings_service
from ..services.vol_service import atm_iv_from_chain, iv_rank_for

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class LegInput(BaseModel):
    kind: Literal["call", "put", "stock"]
    quantity: float = Field(..., description="Signed. Negative = short.")
    strike: float | None = None
    dte: float = 0
    entry_price: float | None = Field(
        None, description="Per-share fill. Omit to use the model/chain mid.")
    iv: float | None = None
    multiplier: float | None = None


class AnalyzeRequest(BaseModel):
    symbol: str = "SPY"
    template: str | None = Field(
        None, description="Template id. Omit when supplying explicit legs.")
    params: dict[str, Any] = Field(default_factory=dict)
    legs: list[LegInput] | None = None

    spot: float | None = Field(None, description="Override the live spot price.")
    rate: float | None = None
    dividend_yield: float = 0.0
    iv_override: float | None = Field(
        None, description="Flat IV to price every leg at, replacing the surface.")
    commission_per_contract: float | None = None
    american: bool = Field(
        False,
        description="Bjerksund-Stensland American valuation instead of "
                    "Black-Scholes. More correct for deep-ITM puts, much slower "
                    "to render.")

    use_live_chain: bool = True
    provider: str | None = None

    measure: Literal["risk-neutral", "real-world"] = "risk-neutral"
    expected_return: float = 0.0
    include_monte_carlo: bool = False
    include_surface: bool = True
    include_earnings: bool = Field(
        True, description="Check whether an announcement falls inside the expiry "
                          "window and price the event move.")
    curve_points: int = Field(161, ge=41, le=601)
    day_offsets: list[float] | None = None
    iv_shift: float = 0.0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _sanitize(value):
    """
    Make a payload JSON-safe.

    Two jobs. First, JSON has no representation for inf or NaN: unbounded risk
    is a real state, so it becomes null paired with an explicit *_unbounded
    flag rather than letting a non-conforming float escape.

    Second, numpy scalars. The forecasting and simulation modules return
    np.float64 and np.bool_, which are NOT instances of float and bool and which
    the JSON encoder rejects with a singularly unhelpful error. Converting them
    here means no caller has to remember to.
    """
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, np.ndarray):
        return [_sanitize(v) for v in value.tolist()]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


async def _build_context(req: AnalyzeRequest) -> tuple[MarketContext, dict]:
    settings = get_settings()
    registry = get_registry()
    meta: dict = {"data_source": "synthetic", "chain_used": False,
                  "warnings": []}

    spot = req.spot
    surface = None
    increment = 1.0

    chain: OptionChain | None = None
    if req.use_live_chain:
        try:
            chain = await registry.chain(req.symbol, max_expirations=8,
                                         provider=req.provider)
            meta["data_source"] = chain.source
            meta["chain_used"] = True
            if spot is None:
                spot = chain.spot
        except ProviderError as exc:
            meta["warnings"].append(f"chain unavailable: {exc}")

    if spot is None:
        try:
            quote = await registry.quote(req.symbol, provider=req.provider)
            spot = quote.last
            meta["data_source"] = quote.source
        except ProviderError as exc:
            raise HTTPException(502, f"could not price {req.symbol}: {exc}") from exc

    fallback = ParametricVolSurface(spot=spot, atm_vol=req.iv_override or 0.28)

    if req.iv_override:
        surface = ParametricVolSurface(spot=spot, atm_vol=req.iv_override,
                                       skew=0.0, smile=0.0, term_slope=0.0)
    elif chain and chain.contracts:
        cs = ChainVolSurface(spot=spot, fallback=fallback)
        strikes = set()
        for c in chain.contracts:
            if c.implied_vol:
                cs.add(c.dte, c.strike, c.implied_vol)
            strikes.add(c.strike)
        surface = cs.finalize()
        ordered = sorted(strikes)
        gaps = [round(b - a, 4) for a, b in zip(ordered, ordered[1:]) if b > a]
        if gaps:
            increment = min(gaps)
        if not cs.points:
            meta["warnings"].append(
                "chain returned no implied volatilities; using the modelled "
                "surface instead")
    else:
        surface = fallback

    ctx = MarketContext(
        spot=spot, surface=surface,
        rate=req.rate if req.rate is not None else settings.risk_free_rate,
        dividend_yield=req.dividend_yield,
        strike_increment=increment if increment > 0 else 1.0,
        american=req.american, symbol=req.symbol.upper(),
    )

    # Prefer real chain mids over model prices where a quote exists.
    if chain:
        for c in chain.contracts:
            mid = c.mid
            if mid and mid > 0:
                ctx.quotes[(round(float(c.dte), 3), round(c.strike, 4), c.right)] = mid

    meta["spot"] = spot
    meta["strike_increment"] = ctx.strike_increment
    return ctx, meta


def _position_from_legs(req: AnalyzeRequest, ctx: MarketContext) -> Position:
    settings = get_settings()
    legs: list[Leg] = []
    for spec in req.legs or []:
        if spec.kind == "stock":
            legs.append(Leg(kind="stock", quantity=spec.quantity,
                            entry_price=spec.entry_price or ctx.spot,
                            multiplier=1.0, symbol=ctx.symbol))
            continue
        if spec.strike is None:
            raise HTTPException(422, "option legs require a strike")
        strike = ctx.round_strike(spec.strike)
        iv = spec.iv or ctx.iv_for(strike, spec.dte, spec.kind)
        price = spec.entry_price
        if price is None:
            price = ctx.theo(spec.kind, strike, spec.dte)
        legs.append(Leg(kind=spec.kind, quantity=spec.quantity,
                        entry_price=price, strike=strike, dte=spec.dte, iv=iv,
                        multiplier=spec.multiplier or 100.0,
                        american=ctx.american, symbol=ctx.symbol))
    if not legs:
        raise HTTPException(422, "supply either a template id or at least one leg")

    return Position(
        legs=legs, spot=ctx.spot, rate=ctx.rate,
        dividend_yield=ctx.dividend_yield, name="Custom structure",
        commission_per_contract=(req.commission_per_contract
                                 if req.commission_per_contract is not None
                                 else settings.commission_per_contract),
        metadata={"symbol": ctx.symbol, "template": None},
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@router.get("/templates")
async def templates(category: str | None = None, outlook: str | None = None):
    return {
        "categories": categories(),
        "templates": list_templates(category=category, outlook=outlook),
    }


@router.get("/templates/{template_id}")
async def template_detail(template_id: str):
    try:
        return get_template(template_id).to_meta()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/analyze")
async def analyze_strategy(req: AnalyzeRequest):
    """
    The core endpoint. Builds a position from a template or explicit legs,
    prices it against the live chain where available, and returns the full
    analytic block plus every curve the risk graph needs.
    """
    ctx, meta = await _build_context(req)
    settings = get_settings()

    if req.template:
        try:
            template = get_template(req.template)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        position = template.make(ctx, req.params)
        position.commission_per_contract = (
            req.commission_per_contract if req.commission_per_contract is not None
            else settings.commission_per_contract)
        template_meta = {
            "id": template.id, "name": template.name,
            "summary": template.summary, "mechanics": template.mechanics,
            "when_to_use": template.when_to_use, "risk_notes": template.risk_notes,
            "management": template.management, "category": template.category,
            "outlook": template.outlook, "complexity": template.complexity,
            "tags": template.tags,
        }
    else:
        position = _position_from_legs(req, ctx)
        template_meta = None

    analysis = analyze(position, measure=req.measure,
                       expected_return=req.expected_return,
                       include_monte_carlo=req.include_monte_carlo)

    earnings_ctx = None
    if req.include_earnings and position.max_dte > 0:
        earnings_ctx = await _earnings_context(ctx.symbol, position.max_dte)

    prices = position.price_grid(points=req.curve_points)
    payload = {
        "meta": meta,
        "template": template_meta,
        "position": {
            "name": position.name,
            "symbol": ctx.symbol,
            "legs": position.leg_greeks(),
            "net_debit": position.net_debit,
            "net_credit": position.net_credit,
            "pricing_model": "american-bjerksund-stensland" if req.american
                             else "black-scholes-merton",
        },
        "analysis": analysis,
        "curves": {
            "prices": prices,
            "expiry": position.expiry_pl_curve(prices),
            "theoretical": position.theoretical_curves(
                day_offsets=req.day_offsets, prices=prices,
                iv_shift=req.iv_shift),
        },
        "surface": position.pl_surface() if req.include_surface else None,
        "earnings": earnings_ctx,
        "insights": [i.to_dict() for i in
                     evaluate(position, analysis, earnings=earnings_ctx)],
    }
    return _sanitize(payload)


async def _earnings_context(symbol: str, dte: float) -> dict | None:
    """
    Whether an announcement falls inside the expiry, and what the chain charges
    for it. Failures here are non-fatal: a missing earnings feed should degrade
    the guidance, never break the risk graph.
    """
    try:
        in_window = await earnings_service.events_in_window(symbol, dte)
    except Exception:                                          # noqa: BLE001
        return None
    if not in_window:
        return {"events_in_window": [], "has_event": False}

    payload: dict = {
        "has_event": True,
        "events_in_window": [e.to_dict() for e in in_window],
        "implied_move_pct": None,
        "verdict": None,
        "history_count": 0,
        "synthetic": any(e.is_synthetic for e in in_window),
    }

    try:
        report = await earnings_service.earnings_report(symbol)
        move = report.get("implied_move") or {}
        block = (move.get("term_structure")
                 if move.get("preferred") == "term-structure"
                 else move.get("straddle")) or {}
        payload["implied_move_pct"] = block.get("expected_move_pct")
        payload["implied_move_method"] = move.get("preferred")
        payload["implied_move"] = move
        payload["verdict"] = report.get("verdict")
        payload["history"] = report.get("history")
        payload["history_count"] = (report.get("history") or {}).get("count", 0)
    except Exception:                                          # noqa: BLE001
        pass

    return payload


@router.post("/compare")
async def compare_strategies(req: AnalyzeRequest,
                             templates: list[str] = Query(...)):
    """
    Run several structures against the same market snapshot. This is the honest
    way to choose between, say, a short strangle and an iron condor: identical
    spot, identical surface, identical DTE, and the differences are entirely
    structural.
    """
    ctx, meta = await _build_context(req)
    rows = []
    for tid in templates[:8]:
        try:
            template = get_template(tid)
        except KeyError:
            continue
        position = template.make(ctx, req.params)
        a = analyze(position, measure=req.measure,
                    expected_return=req.expected_return)
        rows.append({
            "id": tid, "name": template.name,
            "net_debit": position.net_debit,
            "max_profit": a["max_profit"], "max_loss": a["max_loss"],
            "max_loss_unbounded": a["max_loss_unbounded"],
            "probability_of_profit": a["probability_of_profit"],
            "expected_value": a["expected_value"],
            "risk_reward": a["risk_reward"],
            "return_on_capital": a["return_on_capital"],
            "capital": a["capital"]["estimated_buying_power"],
            "breakevens": a["breakevens"],
            "net_greeks": a["net_greeks"],
            "risk_defined": a["risk_defined"],
        })
    return _sanitize({"meta": meta, "results": rows})


@router.post("/guidance")
async def guidance(req: AnalyzeRequest,
                   outlook: Literal["bullish", "bearish", "neutral", "volatile"]
                   = "neutral",
                   risk_defined_only: bool = True):
    """Rank the template library against a market view and the current IV regime."""
    ctx, meta = await _build_context(req)
    registry = get_registry()

    rank = None
    rank_detail = None
    try:
        chain = await registry.chain(req.symbol, provider=req.provider)
        ivs = [c.implied_vol for c in chain.contracts
               if c.implied_vol and 20 <= c.dte <= 60]
        atm = atm_iv_from_chain(chain, 30) or ctx.surface.iv(ctx.spot, 30)
        if atm:
            rank_detail = await iv_rank_for(req.symbol, atm, ivs)
            rank = rank_detail.get("iv_rank")
    except ProviderError:
        pass

    return _sanitize({
        "meta": meta,
        "outlook": outlook,
        "iv_rank": rank,
        "iv_rank_detail": rank_detail,
        "suggestions": suggest_strategies(ctx, outlook=outlook, iv_rank=rank,
                                          risk_defined_only=risk_defined_only),
    })
