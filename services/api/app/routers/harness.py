"""Adaptive strategy-harness API.

This router owns the protocol layer between conversational research and a
strategy that is ready for a *paper* workflow. It never places an order and it
does not silently promote a result: evidence is append-only and promotion is a
separate, explicit decision.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..agentics import SessionDB

router = APIRouter(prefix="/api/harness", tags=["adaptive-harness"])

_HOME = Path(__file__).resolve().parents[2] / ".hermes"
_db: SessionDB | None = None


def get_harness_store() -> SessionDB:
    global _db
    if _db is None:
        _db = SessionDB(_HOME / "state.db")
    return _db


class JourneyRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=160)
    symbol: str | None = Field(default=None, max_length=20)
    thesis: str | None = Field(default=None, max_length=4000)
    session_id: str | None = Field(default=None, max_length=64)
    template_id: str | None = Field(default=None, max_length=80)


class ExperimentRequest(BaseModel):
    kind: str = Field(..., pattern="^(backtest|dojo|forward_test|paper_review|research)$")
    template_id: str | None = Field(default=None, max_length=80)
    hypothesis: str | None = Field(default=None, max_length=4000)
    parameters: dict = Field(default_factory=dict)


class EvidenceRequest(BaseModel):
    source: str = Field(..., min_length=2, max_length=80)
    mode: str = Field(..., pattern="^(modelled|option_quotes|walk_forward|monte_carlo|paper|research)$")
    summary: str = Field(..., min_length=3, max_length=8000)
    metrics: dict = Field(default_factory=dict)
    caveat: str | None = Field(default=None, max_length=4000)


class DecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(paper_ready|rejected|hold)$")
    note: str | None = Field(default=None, max_length=4000)


class DailyBriefRequest(BaseModel):
    content: str = Field(..., min_length=3, max_length=16000)
    symbol: str | None = Field(default=None, max_length=20)
    inputs: dict = Field(default_factory=dict)


@router.get("/overview")
async def overview(limit: int = Query(default=12, ge=1, le=100)):
    return get_harness_store().harness_overview(limit=limit)


@router.get("/journeys")
async def journeys(symbol: str | None = Query(default=None, max_length=20),
                    limit: int = Query(default=20, ge=1, le=100)):
    return {"journeys": get_harness_store().list_journeys(symbol=symbol, limit=limit)}


@router.post("/journeys")
async def create_journey(req: JourneyRequest):
    return get_harness_store().create_journey(
        name=req.name, symbol=req.symbol.upper() if req.symbol else None,
        thesis=req.thesis, source_session_id=req.session_id,
        meta={"template_id": req.template_id} if req.template_id else {},
    )


@router.post("/journeys/{journey_id}/experiments")
async def create_experiment(journey_id: str, req: ExperimentRequest):
    store = get_harness_store()
    if not store.get_journey(journey_id):
        raise HTTPException(404, "strategy journey not found")
    return store.create_experiment(journey_id=journey_id, kind=req.kind,
                                   template_id=req.template_id,
                                   hypothesis=req.hypothesis,
                                   parameters=req.parameters)


@router.post("/experiments/{experiment_id}/evidence")
async def add_evidence(experiment_id: str, req: EvidenceRequest):
    store = get_harness_store()
    if not store.get_experiment(experiment_id):
        raise HTTPException(404, "strategy experiment not found")
    return store.add_evidence(experiment_id=experiment_id, source=req.source,
                              mode=req.mode, summary=req.summary,
                              metrics=req.metrics, caveat=req.caveat)


@router.post("/experiments/{experiment_id}/decision")
async def decide(experiment_id: str, req: DecisionRequest):
    store = get_harness_store()
    if not store.get_experiment(experiment_id):
        raise HTTPException(404, "strategy experiment not found")
    return store.decide_experiment(experiment_id, decision=req.decision, note=req.note)


@router.post("/daily-brief")
async def add_daily_brief(req: DailyBriefRequest):
    """Persists a generated/scheduled brief; scheduling itself stays external."""
    return get_harness_store().create_daily_brief(
        brief_date=dt.date.today().isoformat(), content=req.content,
        symbol=req.symbol.upper() if req.symbol else None, inputs=req.inputs)
