"""Live mode must never fall back to plausible-looking generated prices."""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.providers.base import ProviderError
from app.providers.registry import ProviderRegistry


def test_live_mode_does_not_register_synthetic_fallbacks():
    registry = ProviderRegistry(Settings(market_data_mode="live"))

    assert registry.status() == []
    assert registry.earnings_status() == []
    assert registry.preferred() == "unconfigured"
    readiness = registry.readiness()
    assert readiness["mode"] == "live"
    assert readiness["live_ready"] is False
    assert "MASSIVE_API_KEY" in readiness["message"]


def test_live_mode_explains_how_to_configure_market_data():
    registry = ProviderRegistry(Settings(market_data_mode="live"))

    with pytest.raises(ProviderError, match="Live market data is not configured"):
        asyncio.run(registry.quote("NVDA"))


def test_demo_mode_keeps_the_explicit_test_fixture():
    registry = ProviderRegistry(Settings(market_data_mode="demo"))

    assert registry.preferred() == "synthetic"
    assert registry.status()[-1]["synthetic"] is True
