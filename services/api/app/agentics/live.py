"""Live LLM narration for the MarketShark agent.

This is a deliberately narrow adaptation of the Hermes v0.19 pattern: inject
durable memory and applicable skills before a turn, persist the resulting
session, and keep execution behind an auditable tool boundary.  The model is
never given order-entry capability.  Deterministic MarketShark tools remain the
source of every market number and strategy result.
"""

from __future__ import annotations

import json

import httpx

from ..config import Settings
from .memory import MemoryStore
from .skills import SkillLibrary


class LiveModelError(RuntimeError):
    """The configured model could not complete a turn."""


def provider_name(settings: Settings) -> str:
    """Return the configured model gateway without leaking credentials."""
    return (settings.ai_provider or "local").strip().lower()


def is_configured(settings: Settings) -> bool:
    """A model gateway is opt-in: no key means rules-only, never a fake LLM."""
    provider = provider_name(settings)
    if provider == "openrouter":
        return bool(settings.openrouter_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    # Preserve the existing local/anthropic configuration as a compatible path.
    return bool(settings.anthropic_api_key) and provider in {"local", "anthropic"}


def allowed_models(settings: Settings) -> list[str]:
    """Configured, server-controlled model choices shown by the chat UI."""
    configured = [model.strip() for model in settings.ai_models if model.strip()]
    primary = settings.ai_model.strip()
    return list(dict.fromkeys([primary, *configured])) if primary else configured


def resolve_model(settings: Settings, requested: str | None = None) -> str:
    models = allowed_models(settings)
    if not models:
        raise LiveModelError("No AI model is configured")
    if requested and requested in models:
        return requested
    return models[0]


async def narrate(
    *,
    settings: Settings,
    user_message: str,
    deterministic_result: str,
    symbol: str | None,
    memory: MemoryStore,
    skills: SkillLibrary,
    memory_context: str | None = None,
    memory_system_prompt: str | None = None,
    requested_model: str | None = None,
) -> str:
    """Ask the configured LLM to explain verified tools and contextual knowledge.

    The model gets no credentials, no shell, no browser, and no order tool.  It
    may clarify an unmatched question, but must not manufacture measurements;
    verified output is supplied separately and is explicitly authoritative.
    """
    if not is_configured(settings):
        raise LiveModelError("No live model is configured")

    memory_block = memory_context if memory_context is not None else memory.inject(
        {"message": user_message, "symbol": symbol or ""})
    matching = skills.match(user_message, subject=symbol, limit=3)
    skill_block = "\n".join(
        f"- {skill.name} ({skill.status}, {skill.verdict()})\n{skill.body[:600]}"
        for skill in matching
    ) or "No applicable validated skill was found."
    verified = deterministic_result or "No MarketShark tool ran for this turn."
    system = """You are MarketShark's research copilot. You help a trader reason about
options and market research, but you never place, route, or imply an ability to
place orders. Treat the VERIFIED TOOL RESULT as authoritative for numbers and
events. Do not invent prices, historical results, risk measures, or claims of
having run a tool. Treat memory as fallible and label an uncertain preference as
such. Keep the response useful and concise. Include a short 'Decision trace:'
with the verified inputs used, rather than hidden chain-of-thought."""
    if memory_system_prompt:
        system += "\n\nMemory protocol:\n" + memory_system_prompt[:1800]
    prompt = f"""User request:\n{user_message[:12000]}\n\nSymbol context: {symbol or 'none'}

VERIFIED TOOL RESULT:\n{verified[:12000]}

{memory_block or 'No durable memory is relevant.'}

## Applicable strategy skills\n{skill_block}

Respond to the user now. Do not mention this prompt or pretend the skills are proven when their score says otherwise."""
    model = resolve_model(settings, requested_model)
    provider = provider_name(settings)
    payload = {
        "model": model,
        "max_tokens": 1200,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    endpoint = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    parser = "anthropic"
    if provider in {"openrouter", "openai"}:
        # OpenRouter intentionally implements the OpenAI chat-completions
        # contract, so the only difference is endpoint, credential, and its
        # optional attribution headers. Keys never leave this server.
        payload = {
            "model": model,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if provider == "openrouter":
            endpoint = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "authorization": f"Bearer {settings.openrouter_api_key}",
                "content-type": "application/json",
                "http-referer": "http://localhost:3100",
                "x-title": "MarketShark Trader",
            }
        else:
            endpoint = "https://api.openai.com/v1/chat/completions"
            headers = {"authorization": f"Bearer {settings.openai_api_key}",
                       "content-type": "application/json"}
        parser = "openai"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise LiveModelError(f"model network error: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text[:500]
        raise LiveModelError(f"model returned HTTP {response.status_code}: {detail}")
    try:
        body = response.json()
        if parser == "openai":
            text = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
        else:
            blocks = body.get("content", [])
            text = "\n".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text").strip()
    except (ValueError, AttributeError) as exc:
        raise LiveModelError("model returned an invalid response") from exc
    if not text:
        raise LiveModelError("model returned no text")
    return text


def tool_snapshot(tool_calls: list[dict]) -> str:
    """Compact, provenance-preserving payload for durable measured memory."""
    return json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":"))[:1200]
