"""
Provider registry and failover chain.

Builds providers from configuration and routes each call down an ordered chain,
falling through to the next provider on failure. In explicit demo mode the
synthetic provider sits at the end. In live mode it is never registered: a
missing credential, entitlement, or upstream outage must be visible rather
than replaced with plausible-looking generated prices.

Every response records which provider actually served it, so the UI can label
synthetic data honestly rather than passing it off as market data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Callable

from ..config import Settings, get_settings
from .base import (
    Bar,
    CompanyProfile,
    EarningsEvent,
    Fundamentals,
    NewsItem,
    OptionChain,
    ProviderError,
    Quote,
)
from .earnings_sources import (
    BenzingaEarningsProvider,
    FinnhubEarningsProvider,
    NasdaqEarningsProvider,
    SyntheticEarningsProvider,
)
from .synthetic import SyntheticProvider

log = logging.getLogger(__name__)


class _TTLCache:
    """
    Small in-process cache. Chains are expensive and rate limits are tight --
    Massive's free tier is 5 requests a minute and Webull's option snapshot is
    60 -- so re-fetching on every keystroke in the strategy builder would break
    the product within seconds of a user touching a slider.
    """

    def __init__(self):
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        hit = self._data.get(key)
        if not hit:
            return None
        stamped, value = hit
        if time.monotonic() - stamped > ttl:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


class ProviderRegistry:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.cache = _TTLCache()
        self._providers: list[Any] = []
        self._earnings: list[Any] = []
        self._build()
        self._build_earnings()

    def _build(self) -> None:
        s = self.settings
        chain: list[Any] = []

        if s.massive_api_key:
            try:
                from .massive import MassiveProvider
                chain.append(MassiveProvider(
                    s.massive_api_key, base_url=s.massive_base_url,
                    is_realtime=s.massive_realtime))
                log.info("Massive provider enabled")
            except Exception as exc:
                log.warning("Massive provider unavailable: %s", exc)

        if s.webull_app_key and s.webull_app_secret:
            try:
                from .webull import WebullProvider
                chain.append(WebullProvider(
                    s.webull_app_key, s.webull_app_secret,
                    region=s.webull_region, rate=s.risk_free_rate))
                log.info("Webull provider enabled (region=%s)", s.webull_region)
            except Exception as exc:
                log.warning("Webull provider unavailable: %s", exc)

        if s.allows_synthetic_data:
            chain.append(SyntheticProvider(rate=s.risk_free_rate))
        self._providers = chain

    def _build_earnings(self) -> None:
        s = self.settings
        chain: list[Any] = []

        if s.massive_api_key and s.benzinga_earnings:
            try:
                chain.append(BenzingaEarningsProvider(
                    s.massive_api_key, base_url=s.massive_base_url))
                log.info("Benzinga earnings enabled")
            except Exception as exc:                       # noqa: BLE001
                log.warning("Benzinga earnings unavailable: %s", exc)

        if s.finnhub_token:
            try:
                chain.append(FinnhubEarningsProvider(s.finnhub_token))
                log.info("Finnhub earnings enabled")
            except Exception as exc:                       # noqa: BLE001
                log.warning("Finnhub earnings unavailable: %s", exc)

        if s.earnings_allow_nasdaq:
            chain.append(NasdaqEarningsProvider())
            log.info("Nasdaq earnings calendar enabled (undocumented endpoint)")

        if s.allows_synthetic_data:
            chain.append(SyntheticEarningsProvider())
        self._earnings = chain

    def earnings_status(self) -> list[dict]:
        return [{"name": p.name, "synthetic": p.name == "synthetic"}
                for p in self._earnings]

    def readiness(self) -> dict:
        """Safe, browser-facing data readiness without revealing secrets."""
        market = self.status()
        earnings = self.earnings_status()
        return {
            "mode": "live" if self.settings.live_mode else "demo",
            "live_ready": self.settings.live_ready,
            "market_ready": bool(market and any(not row["synthetic"] for row in market)),
            "earnings_ready": bool(earnings and any(not row["synthetic"] for row in earnings)),
            "message": (
                "Live market data is ready. Vendor entitlements are checked on each request."
                if self.settings.live_ready else
                "Live market data is not configured. Set MASSIVE_API_KEY or WEBULL_APP_KEY and "
                "WEBULL_APP_SECRET, then restart the API."
                if self.settings.live_mode else
                "Demo mode is using generated data; it is not market data."
            ),
        }

    async def earnings(self, symbol: str, *, lookback_quarters: int = 8,
                       lookahead_days: int = 120,
                       provider: str | None = None) -> list[EarningsEvent]:
        key = f"earnings:{symbol.upper()}:{lookback_quarters}:{lookahead_days}"
        cache_key = f"{provider or 'auto'}:{key}"
        hit = self.cache.get(cache_key, self.settings.earnings_cache_seconds)
        if hit is not None:
            return hit

        candidates = self._earnings
        if provider:
            candidates = [p for p in self._earnings if p.name == provider] or candidates

        errors: list[str] = []
        for p in candidates:
            try:
                value = await p.get_earnings(symbol,
                                             lookback_quarters=lookback_quarters,
                                             lookahead_days=lookahead_days)
                if value:
                    self.cache.put(cache_key, value)
                    return value
                errors.append(f"{p.name}: no events returned")
            except ProviderError as exc:
                errors.append(f"{p.name}: {exc}")
            except Exception as exc:                       # noqa: BLE001
                errors.append(f"{p.name}: unexpected {exc!r}")
                log.exception("earnings provider %s raised", p.name)

        if not candidates:
            raise ProviderError(
                "Live earnings are not configured. Set FINNHUB_TOKEN or enable the "
                "Benzinga earnings entitlement, then restart the API.",
                provider="configuration")
        raise ProviderError("every earnings source failed: " + "; ".join(errors))

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._providers]

    def status(self) -> list[dict]:
        return [{
            "name": p.name,
            "supports_greeks": getattr(p, "supports_greeks", False),
            "realtime": getattr(p, "is_realtime", False),
            "synthetic": p.name == "synthetic",
        } for p in self._providers]

    def preferred(self) -> str:
        return self._providers[0].name if self._providers else "unconfigured"

    # ---- dispatch --------------------------------------------------------

    async def _dispatch(self, method: str, *args, provider: str | None = None,
                        **kwargs):
        candidates = self._providers
        if provider:
            candidates = [p for p in self._providers if p.name == provider] or candidates

        if not candidates:
            raise ProviderError(
                "Live market data is not configured. Set MASSIVE_API_KEY or WEBULL_APP_KEY and "
                "WEBULL_APP_SECRET, then restart the API.",
                provider="configuration")

        errors: list[str] = []
        for p in candidates:
            fn: Callable | None = getattr(p, method, None)
            if fn is None:
                continue
            try:
                return await fn(*args, **kwargs)
            except ProviderError as exc:
                errors.append(f"{p.name}: {exc}")
                log.info("provider %s failed %s -> falling through (%s)",
                         p.name, method, exc)
            except Exception as exc:                       # noqa: BLE001
                errors.append(f"{p.name}: unexpected {exc!r}")
                log.exception("provider %s raised on %s", p.name, method)

        raise ProviderError(
            f"every provider failed for {method}: " + "; ".join(errors))

    async def _cached(self, key: str, ttl: float, method: str, *args,
                      provider: str | None = None, **kwargs):
        cache_key = f"{provider or 'auto'}:{key}"
        hit = self.cache.get(cache_key, ttl)
        if hit is not None:
            return hit
        value = await self._dispatch(method, *args, provider=provider, **kwargs)
        self.cache.put(cache_key, value)
        return value

    # ---- public API ------------------------------------------------------

    async def quote(self, symbol: str, *, provider: str | None = None) -> Quote:
        return await self._cached(f"quote:{symbol.upper()}",
                                  self.settings.quote_cache_seconds,
                                  "get_quote", symbol, provider=provider)

    async def bars(self, symbol: str, *, timespan: str = "day", limit: int = 250,
                   provider: str | None = None) -> list[Bar]:
        return await self._cached(
            f"bars:{symbol.upper()}:{timespan}:{limit}",
            self.settings.bars_cache_seconds, "get_bars", symbol,
            timespan=timespan, limit=limit, provider=provider)

    async def chain(self, symbol: str, *, expiration: dt.date | None = None,
                    max_expirations: int = 6, strike_window_pct: float = 0.30,
                    provider: str | None = None) -> OptionChain:
        key = (f"chain:{symbol.upper()}:{expiration or 'all'}:"
               f"{max_expirations}:{strike_window_pct}")
        return await self._cached(
            key, self.settings.chain_cache_seconds, "get_option_chain", symbol,
            expiration=expiration, max_expirations=max_expirations,
            strike_window_pct=strike_window_pct, provider=provider)

    async def profile(self, symbol: str, *, provider: str | None = None) -> CompanyProfile:
        return await self._cached(f"profile:{symbol.upper()}", 3600.0,
                                  "get_profile", symbol, provider=provider)

    async def fundamentals(self, symbol: str, *, timeframe: str = "quarterly",
                           limit: int = 8,
                           provider: str | None = None) -> list[Fundamentals]:
        return await self._cached(
            f"fund:{symbol.upper()}:{timeframe}:{limit}", 3600.0,
            "get_fundamentals", symbol, timeframe=timeframe, limit=limit,
            provider=provider)

    async def news(self, symbols: list[str], *, limit: int = 25,
                   provider: str | None = None) -> list[NewsItem]:
        key = f"news:{','.join(sorted(s.upper() for s in symbols))}:{limit}"
        return await self._cached(key, self.settings.news_cache_seconds,
                                  "get_news", symbols, limit=limit,
                                  provider=provider)

    async def aclose(self) -> None:
        for p in [*self._providers, *self._earnings]:
            closer = getattr(p, "aclose", None)
            if closer:
                try:
                    await closer()
                except Exception:                          # noqa: BLE001
                    pass


_registry: ProviderRegistry | None = None
_lock = asyncio.Lock()


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


async def reset_registry() -> None:
    global _registry
    async with _lock:
        if _registry:
            await _registry.aclose()
        _registry = None
