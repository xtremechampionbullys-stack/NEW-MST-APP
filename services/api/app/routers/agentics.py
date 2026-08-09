"""
HTTP surface for the agentic harness: state, memory and skills.

Everything here is a thin wrapper over app/agentics. The endpoints exist so the
harness is inspectable — a memory you cannot read and a skill library you cannot
audit are indistinguishable from ones that do not work.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..agentics import MemoryStore, SessionDB, SkillLibrary
from ..config import get_settings

router = APIRouter(prefix="/api/agentics", tags=["agentics"])

_HOME = Path(__file__).resolve().parents[2] / ".hermes"
_db: SessionDB | None = None


def _state() -> SessionDB:
    global _db
    if _db is None:
        _db = SessionDB(_HOME / "state.db")
    return _db


def _memory() -> MemoryStore:
    return MemoryStore(_HOME)


def _skills() -> SkillLibrary:
    return SkillLibrary(_HOME)


class StoreRequest(BaseModel):
    text: str = Field(..., max_length=2000)
    source: str = "inferred"
    subject: str | None = None
    run_id: str | None = None
    which: str = "memory"


class SkillRequest(BaseModel):
    name: str
    trigger: str
    body: str
    subject: str | None = None


class OutcomeRequest(BaseModel):
    ok: bool
    note: str = ""
    run_id: str | None = None


class ReviseRequest(BaseModel):
    body: str | None = None
    trigger: str | None = None
    reason: str = ""


@router.get("/status")
async def status():
    mem = _memory()
    skl = _skills().report()
    return {
        "home": str(_HOME),
        "state": _state().stats(),
        "memory": {"facts": len(mem.load()), "user_facts": len(mem.load("user"))},
        "skills": {"total": skl["total"], "by_status": skl["by_status"]},
        "model_configured": bool(get_settings().anthropic_api_key),
        "note": ("Session state, memory and skills modelled on the published Hermes "
                 "Agent architecture. Facts carry how they were learned and decay "
                 "unless stated; skills carry a scoreboard and are demoted when the "
                 "evidence says they are not working."),
    }


# -- memory ---------------------------------------------------------------

@router.get("/memory")
async def memory(which: str = "memory"):
    facts = _memory().load(which)
    return {"which": which, "count": len(facts),
            "facts": [{"text": f.text, "source": f.source, "subject": f.subject,
                       "learned_at": f.learned_at, "run_id": f.run_id,
                       "hits": f.hits, "confidence": f.confidence()} for f in facts]}


@router.post("/memory")
async def memory_store(req: StoreRequest):
    f = _memory().store(req.text, source=req.source, subject=req.subject,
                        run_id=req.run_id, which=req.which)
    return {"stored": f.to_line(), "source": f.source, "confidence": f.confidence()}


@router.get("/memory/recall")
async def memory_recall(q: str, limit: int = Query(8, ge=1, le=50)):
    facts = _memory().recall(q, limit=limit)
    return {"query": q, "count": len(facts),
            "facts": [{"text": f.text, "source": f.source,
                       "confidence": f.confidence()} for f in facts],
            "note": ("Facts below 0.15 confidence are withheld, not ranked last — "
                     "a decayed measurement in the context window reads exactly "
                     "like a current one.")}


@router.get("/memory/reflect")
async def memory_reflect():
    return _memory().reflect()


@router.delete("/memory")
async def memory_forget(pattern: str, which: str = "memory"):
    return {"removed": _memory().forget(pattern, which)}


# -- skills ---------------------------------------------------------------

@router.get("/skills")
async def skills():
    return _skills().report()


@router.post("/skills")
async def skill_create(req: SkillRequest):
    s = _skills().create(name=req.name, trigger=req.trigger, body=req.body,
                         subject=req.subject)
    return s.to_dict()


@router.get("/skills/match")
async def skill_match(context: str, subject: str | None = None,
                      limit: int = Query(5, ge=1, le=20)):
    lib = _skills()
    hits = lib.match(context, subject=subject, limit=limit)
    return {"context": context, "count": len(hits),
            "skills": [s.to_dict() for s in hits],
            "note": "Demoted and retired skills are excluded — a measured negative "
                    "result should not stay in the pool."}


@router.post("/skills/{skill_id}/outcome")
async def skill_outcome(skill_id: str, req: OutcomeRequest):
    s = _skills().record(skill_id, req.ok, note=req.note, run_id=req.run_id)
    if not s:
        raise HTTPException(404, "skill not found")
    return s.to_dict()


@router.post("/skills/{skill_id}/revise")
async def skill_revise(skill_id: str, req: ReviseRequest):
    s = _skills().revise(skill_id, body=req.body, trigger=req.trigger, reason=req.reason)
    if not s:
        raise HTTPException(404, "skill not found")
    return s.to_dict()
