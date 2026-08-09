"""Provider-gateway checks: model selection must stay server allow-listed."""

from app.agentics.live import allowed_models, is_configured, provider_name, resolve_model
from app.config import Settings


def test_openrouter_is_enabled_only_with_its_server_key():
    settings = Settings(ai_provider="openrouter", openrouter_api_key="secret", ai_model="openai/gpt-4.1-mini",
                        ai_models=["anthropic/claude-sonnet-4.5"])
    assert is_configured(settings)
    assert provider_name(settings) == "openrouter"
    assert allowed_models(settings) == ["openai/gpt-4.1-mini", "anthropic/claude-sonnet-4.5"]
    assert resolve_model(settings, "anthropic/claude-sonnet-4.5") == "anthropic/claude-sonnet-4.5"
    assert resolve_model(settings, "someone/unknown-model") == "openai/gpt-4.1-mini"


def test_openai_does_not_turn_on_from_an_openrouter_key():
    settings = Settings(ai_provider="openai", openrouter_api_key="router-only", ai_model="gpt-4.1-mini")
    assert not is_configured(settings)
    assert is_configured(Settings(ai_provider="openai", openai_api_key="server-key", ai_model="gpt-4.1-mini"))
