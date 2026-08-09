"""
Agent chat endpoint: routes an utterance, executes the tools, returns both.

The execution half lives here rather than in services/agent.py so that intent
parsing stays a pure function — testable across the whole intent table without a
provider, a network, or a running app.

Every response carries `tool_calls`: what actually ran, with arguments. That is
the difference between an agent and a chat bubble. If the reply claims a
backtest was run, the tool call is in the payload and the caller can check it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..agentics import BuiltinMemoryProvider, MemoryManager, MemoryStore, Message, SessionDB, SkillLibrary
from ..agentics.live import (LiveModelError, allowed_models, is_configured, narrate,
                              provider_name, resolve_model, tool_snapshot)
from ..engine.strategies import REGISTRY
from ..providers.base import ProviderError
from ..services import agent as agent_svc
from .strategy import _sanitize
from .harness import get_harness_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

_HOME = Path(__file__).resolve().parents[2] / ".hermes"
_state_db: SessionDB | None = None
_memory_manager: MemoryManager | None = None


def _state() -> SessionDB:
    global _state_db
    if _state_db is None:
        _state_db = SessionDB(_HOME / "state.db")
    return _state_db


def _memory() -> MemoryStore:
    return _memory_runtime().store


def _memory_runtime() -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        store = MemoryStore(_HOME)
        _memory_manager = MemoryManager(store)
        _memory_manager.add_provider(BuiltinMemoryProvider(store))
    return _memory_manager


def _skills() -> SkillLibrary:
    return SkillLibrary(_HOME)


class ChatRequest(BaseModel):
    # Text attachments are forwarded as visible message context. This allows a
    # few compact research notes without turning chat into opaque file storage.
    message: str = Field(..., max_length=64000)
    symbol: str | None = None
    template: str | None = None
    history: list[dict] = Field(default_factory=list)
    session_id: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=160)


@router.get("/capabilities")
async def capabilities():
    settings = get_settings()
    return {
        "engine": f"{provider_name(settings)} live + rules" if is_configured(settings) else "rules",
        "model_configured": is_configured(settings),
        "provider": provider_name(settings) if is_configured(settings) else None,
        "models": allowed_models(settings) if is_configured(settings) else [],
        "capabilities": [{"label": k, "example": v}
                         for k, v in agent_svc.CAPABILITIES],
        "intents": [{"name": i.name, "description": i.description}
                    for i in agent_svc.INTENTS],
        "can_place_orders": False,
        "note": (
            "This agent is a deterministic intent router, not a language model. "
            "It matches documented phrasings, calls the same endpoints the UI "
            "calls, and returns the real output — no key required. When it does "
            "not match, it says so instead of guessing, because a confident "
            "answer to a question you did not ask is indistinguishable from a "
            "correct one until it costs you money."
            if not is_configured(settings) else
            "A live model is configured. Hermes-inspired durable memory and skills "
            "are injected into each turn; deterministic MarketShark tools remain "
            "the only source of market measurements and cannot place orders."
        ),
    }


def _remember_explicitly(memory: MemoryStore, text: str, symbol: str | None, session_id: str) -> None:
    """Persist only overtly stated preferences, never a model's own prose."""
    if re.match(r"^\s*(?:remember|note that|my (?:strategy|trading plan|preference) is|i prefer)\b", text, re.I):
        memory.store(text[:800], source="stated", subject=symbol, run_id=session_id, which="user")


def _record_harness_evidence(turn, req: ChatRequest, session_id: str) -> None:
    """Append measured strategy work to the adaptive ledger.

    This deliberately happens *after* the endpoint result exists. The agent
    cannot create a success record by talking about a test; it only records an
    object returned by the backtest/Dojo/forward engines. Ledger trouble never
    hides a completed analysis from the user.
    """
    records = (
        ("backtest", "backtest", "modelled"),
        ("dojo", "dojo", "walk_forward"),
        ("forward", "forward_test", "monte_carlo"),
    )
    store = get_harness_store()
    symbol = (turn.symbol or req.symbol or "").upper() or None
    for data_key, kind, mode in records:
        result = (turn.data or {}).get(data_key)
        if not isinstance(result, dict):
            continue
        journey = store.find_active_journey(symbol=symbol, template_id=turn.template)
        if journey is None:
            name = " ".join(part for part in (symbol, turn.template or "strategy", "research") if part)
            journey = store.create_journey(
                name=name, symbol=symbol, thesis=req.message[:4000],
                source_session_id=session_id, meta={"template_id": turn.template},
            )
        experiment = store.create_experiment(
            journey_id=journey["id"], kind=kind, template_id=turn.template,
            hypothesis=req.message[:4000], parameters=(turn.params or {}),
        )
        if data_key == "backtest":
            metrics = result.get("metrics") or {}
            summary = f"{result.get('template_name', turn.template or 'strategy')} backtest on {symbol or 'selected symbol'}"
        elif data_key == "dojo":
            metrics = {"validation": result.get("validation"), "variants_tried": result.get("variants_tried")}
            summary = str(result.get("verdict") or f"Dojo search on {symbol or 'selected symbol'}")
        else:
            metrics = {"outcomes": result.get("outcomes"), "path_dependent": result.get("path_dependent"),
                       "paths": result.get("paths"), "horizon_days": result.get("horizon_days")}
            summary = f"Monte Carlo forward test: {result.get('paths', 0):,} paths over {result.get('horizon_days', 0)} days"
        store.add_evidence(experiment_id=experiment["id"], source=f"marketshark.{data_key}",
                           mode=mode, summary=summary, metrics=metrics,
                           caveat=result.get("caveat"))


async def _finish(turn, req: ChatRequest) -> dict:
    """Apply the Hermes-style memory/session phases around a fully auditable turn."""
    settings = get_settings()
    state = _state()
    memory = _memory()
    memory_runtime = _memory_runtime()
    skills = _skills()
    session_id = req.session_id or state.create_session(
        source="marketshark-web", user_id="local", model=resolve_model(settings, req.model) if is_configured(settings) else "rules",
        title=req.message[:80], meta={"symbol": turn.symbol or req.symbol},
    )
    memory_runtime.initialize_all(session_id, platform="marketshark-web", agent_context="primary",
                                  user_id="local")
    memory_runtime.on_turn_start(session_id, req.message, model=settings.ai_model, platform="marketshark-web")
    memory_context = memory_runtime.prefetch_all(req.message, session_id=session_id)
    _remember_explicitly(memory, req.message, turn.symbol or req.symbol, session_id)
    state.add_message(session_id, Message(role="user", content=req.message[:64000]))

    try:
        _record_harness_evidence(turn, req, session_id)
    except Exception:  # noqa: BLE001
        # An audit trail must never turn a measured answer into a blank screen.
        log.exception("could not record adaptive harness evidence")

    # The model only narrates the deterministic result. It cannot call a broker,
    # write code, browse, or replace a measured result with its own number.
    if is_configured(settings):
        try:
            turn.reply = await narrate(
                settings=settings, user_message=req.message, deterministic_result=turn.reply,
                symbol=turn.symbol or req.symbol, memory=memory, skills=skills,
                memory_context=memory_context, memory_system_prompt=memory_runtime.build_system_prompt(),
                requested_model=req.model,
            )
            turn.engine = f"hermes memory + {provider_name(settings)}"
        except LiveModelError as exc:
            turn.caveat = (turn.caveat + "\n" if turn.caveat else "") + f"Live model unavailable; showing verified rules output. ({exc})"

    if turn.tool_calls:
        snapshot = tool_snapshot(turn.tool_calls)
        state.add_message(session_id, Message(role="tool", content=snapshot,
                                              tool_name="marketshark", tool_calls=turn.tool_calls))
    state.add_message(session_id, Message(role="assistant", content=turn.reply[:64000],
                                          tool_calls=turn.tool_calls))
    memory_runtime.sync_all(req.message, turn.reply, session_id=session_id,
                            messages=state.messages(session_id))
    memory_runtime.queue_prefetch_all(turn.reply, session_id=session_id)
    payload = _sanitize(turn.to_dict())
    payload["session_id"] = session_id
    return payload


@router.post("/chat")
async def chat(req: ChatRequest):
    turn = agent_svc.route(req.message,
                           context={"symbol": req.symbol, "template": req.template})

    if not turn.understood or turn.reply:
        return await _finish(turn, req)

    from . import backtest as bt_router
    from . import intelligence as intel
    from . import library, market, strategy

    sym = turn.symbol
    tpl = turn.template
    p = turn.params

    try:
        if turn.intent == "quote":
            q = await market.quote(sym, provider=None)
            turn.tool_calls.append({"tool": "market.quote", "args": {"symbol": sym}})
            turn.data["quote"] = q
            chg = q.get("change_pct")
            turn.reply = (f"**{sym}** {q['last']:,.2f}"
                          + (f" ({chg:+.2f}% on the day)" if chg is not None else ""))
            turn.actions.append({"type": "set_symbol", "symbol": sym})

        elif turn.intent == "analyze":
            tpl = tpl or agent_svc.default_template_for(p.get("outlook"))
            if tpl not in REGISTRY:
                raise HTTPException(422, f"unknown strategy template {tpl}")
            body = strategy.AnalyzeRequest(
                symbol=sym, template=tpl,
                params={k: v for k, v in p.items()
                        if k in {"delta", "width", "contracts"}},
                include_earnings=True,
            )
            if p.get("dte"):
                body.params["dte"] = p["dte"]
            res = await strategy.analyze_strategy(body)
            turn.tool_calls.append({"tool": "strategy.analyze",
                                    "args": {"symbol": sym, "template": tpl,
                                             "params": body.params}})
            turn.template = tpl
            turn.data["analysis"] = res
            turn.reply = agent_svc.describe_analysis(sym, tpl, res["analysis"])
            turn.actions.append({"type": "open_position", "to": "/lab",
                                 "symbol": sym, "template": tpl,
                                 "params": body.params})

        elif turn.intent == "suggest":
            body = strategy.AnalyzeRequest(symbol=sym, template="iron_condor")
            res = await strategy.guidance(body, outlook=p.get("outlook") or "neutral")
            turn.tool_calls.append({"tool": "strategy.guidance",
                                    "args": {"symbol": sym,
                                             "outlook": p.get("outlook") or "neutral"}})
            turn.data["guidance"] = res
            top = (res.get("suggestions") or [])[:3]
            if top:
                lines = "\n".join(
                    f"{i + 1}. **{s['name']}** — {s['summary']}" for i, s in enumerate(top))
                turn.reply = (f"For a {p.get('outlook') or 'neutral'} view on {sym}:\n\n"
                              f"{lines}\n\nThese are ranked by fit to the current "
                              f"surface — IV rank, skew, days to earnings — not by "
                              f"expected profit. Nothing here forecasts direction.")
            else:
                turn.reply = f"No template scored well enough on {sym} to suggest."

        elif turn.intent == "backtest":
            tpl = tpl or "short_strangle"
            rules = {"template_id": tpl, "target_dte": p.get("dte", 45),
                     "params": {k: v for k, v in p.items() if k in {"delta", "width"}}}
            res = await bt_router.run_backtest_endpoint(bt_router.RunRequest(
                symbol=sym, rules=rules, lookback_days=900, persist=False))
            turn.tool_calls.append({"tool": "backtest.run",
                                    "args": {"symbol": sym, "rules": rules}})
            m = res["metrics"]
            turn.template = tpl
            turn.data["backtest"] = res
            turn.reply = (
                f"**{res['template_name']} on {sym}**, {res['start']} → {res['end']}. "
                f"{m['trades']} trades, {m['win_rate']:.0%} win rate, "
                f"${m['total_pl']:,.0f} total, max drawdown ${abs(m['max_drawdown']):,.0f}. "
                f"\n\n{res['caveat']}")
            turn.caveat = res["caveat"]
            turn.actions.append({"type": "navigate", "to": "/backtest", "symbol": sym})

        elif turn.intent == "forward_test":
            tpl = tpl or "short_strangle"
            res = await intel.simulate(intel.ForwardRequest(
                symbol=sym, template=tpl, paths=4000))
            turn.tool_calls.append({"tool": "forward.simulate",
                                    "args": {"symbol": sym, "template": tpl}})
            o, pd_ = res["outcomes"], res["path_dependent"]
            turn.template = tpl
            turn.data["forward"] = res
            turn.reply = (
                f"**{sym} {tpl.replace('_', ' ')}**, {res['paths']:,} paths over "
                f"{res['horizon_days']} days.\n\n"
                f"Win rate {o['win_rate']:.1%}, mean ${o['mean_pl']:,.0f}, "
                f"5th percentile ${o['p05']:,.0f}, worst path ${o['worst']:,.0f}. "
                f"Target hit {pd_['hit_profit_target']:.0%} of the time, stopped "
                f"{pd_['hit_stop_loss']:.0%}.\n\nWinners go ${abs(pd_['mean_max_adverse_on_winners'] or 0):,.0f} "
                f"underwater on average before they work — that number surprises "
                f"people more than the win rate does.")
            turn.actions.append({"type": "navigate", "to": "/research", "symbol": sym,
                                 "tab": "forward"})

        elif turn.intent == "dojo":
            tpl = tpl or "short_strangle"
            res = await intel.dojo_run(intel.DojoRequest(
                symbol=sym, template_id=tpl, variants=30, lookback_days=1200))
            turn.tool_calls.append({"tool": "dojo.run",
                                    "args": {"symbol": sym, "template_id": tpl}})
            v = res["validation"]
            turn.data["dojo"] = res
            turn.reply = (f"Searched {res['variants_tried']} rule sets on {sym}.\n\n"
                          f"{res['verdict']}")
            turn.caveat = res["caveat"]
            turn.actions.append({"type": "navigate", "to": "/research", "symbol": sym,
                                 "tab": "dojo"})

        elif turn.intent == "forecast_vol":
            res = await intel.volatility_forecast(sym, horizons="7,14,30,60,90",
                                                  lookback=900)
            turn.tool_calls.append({"tool": "forecast.volatility", "args": {"symbol": sym}})
            turn.data["volatility"] = res
            proj = res.get("projections") or []
            har = res.get("har") or {}
            r2 = har.get("out_of_sample_r2")
            skill = ("The model shows out-of-sample skill on this name "
                     f"(R² {r2:.2f}), so the level is model-driven."
                     if (r2 or 0) > 0.05 else
                     "The model shows no out-of-sample skill on this name, so the "
                     "level falls back to trailing realized volatility. That is "
                     "reported rather than hidden.")
            if proj:
                rows = " · ".join(f"{q['days']}d {q['projected_vol']:.0%}" for q in proj[:4])
                turn.reply = f"**{sym} projected volatility** — {rows}.\n\n{skill}"
            else:
                turn.reply = f"Not enough history to project volatility for {sym}."
            turn.actions.append({"type": "navigate", "to": "/research", "symbol": sym,
                                 "tab": "forecast"})

        elif turn.intent == "direction":
            res = await intel.direction_forecast(sym, horizon_days=p.get("dte", 21),
                                                 lookback=1200)
            turn.tool_calls.append({"tool": "forecast.direction", "args": {"symbol": sym}})
            turn.data["direction"] = res
            turn.reply = (f"**{sym}** — the model says {res['probability_up']:.1%} "
                          f"probability of a higher close in {res['horizon_days']} days.\n\n"
                          f"{res['verdict']}")
            turn.caveat = ("Read the out-of-sample score before the probability."
                           if not res["trustworthy"] else "")
            turn.actions.append({"type": "navigate", "to": "/research", "symbol": sym,
                                 "tab": "forecast"})

        elif turn.intent == "earnings":
            res = await library.earnings(sym, refresh=False, lookback_quarters=8)
            turn.tool_calls.append({"tool": "earnings.report", "args": {"symbol": sym}})
            turn.data["earnings"] = res
            if res.get("reports_earnings") is False:
                turn.reply = f"{sym} is an index or ETF — it has no earnings announcements."
            else:
                ev = res.get("next_event") or {}
                im = (res.get("implied_move") or {})
                pref = im.get(im.get("preferred", "straddle")) or {}
                move = pref.get("expected_move_pct")
                turn.reply = (
                    f"**{sym} reports {ev.get('event_date')}** "
                    f"({ev.get('session')}, {ev.get('days_away')} days away)."
                    + (f" Options imply a ±{move:.1%} move."
                       if move is not None else "")
                    + (f"\n\n{pref.get('note', '')}" if pref.get("note") else ""))

        elif turn.intent == "paper_trade":
            turn.reply = ("To log a paper trade I need the position first. Build it "
                          "with something like “set up a 16 delta iron condor on "
                          f"{sym}”, then say “log this as a paper trade” — the "
                          "engine's prediction is frozen at entry so it can be "
                          "scored against the outcome later.")
            turn.actions.append({"type": "open_position", "to": "/lab", "symbol": sym,
                                 "template": tpl or "iron_condor"})

    except HTTPException as exc:
        turn.reply = f"That didn't work: {exc.detail}"
        turn.understood = True
    except ProviderError as exc:
        turn.reply = f"The data provider refused: {exc}"
    except Exception as exc:                                    # noqa: BLE001
        log.exception("agent tool failed")
        turn.reply = (f"The {turn.intent} tool raised {type(exc).__name__}: {exc}. "
                      f"Nothing was retried silently — the failure is above.")

    turn.symbol = sym
    return await _finish(turn, req)


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str):
    """Commit the Hermes session-boundary extraction before closing a chat."""
    state = _state()
    if state.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Unknown agent session")
    messages = state.messages(session_id)
    _memory_runtime().on_session_end(session_id, messages)
    state.end_session(session_id, reason="user_closed")
    return {"session_id": session_id, "status": "closed", "messages_committed": len(messages)}
