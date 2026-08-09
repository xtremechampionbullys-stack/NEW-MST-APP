"""Application settings, loaded from the environment."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_local_env() -> None:
    """Load the repository `.env` for direct Uvicorn development runs.

    Docker Compose already supplies this file. Local Uvicorn did not, which
    meant a developer could correctly configure `.env` and still silently run
    on synthetic data/in-memory storage. This tiny parser intentionally only
    supports the conventional ``KEY=value`` form; secrets stay in the process
    environment and are never logged or returned by ``public_dict``.
    """
    path = Path(__file__).resolve().parents[3] / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = _env(key)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    app_name: str = "MarketShark Trader API"
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "development"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", True))

    # --- market data ---
    # `live` refuses generated market data.  `demo` is deliberately opt-in for
    # local development and the deterministic test suite; it is never an
    # acceptable production-data substitute.
    market_data_mode: str = field(
        default_factory=lambda: _env("MARKET_DATA_MODE", "demo").lower())
    massive_api_key: str = field(default_factory=lambda: _env("MASSIVE_API_KEY")
                                 or _env("POLYGON_API_KEY"))
    massive_base_url: str = field(
        default_factory=lambda: _env("MASSIVE_BASE_URL", "https://api.massive.com"))
    massive_realtime: bool = field(default_factory=lambda: _env_bool("MASSIVE_REALTIME"))

    webull_app_key: str = field(default_factory=lambda: _env("WEBULL_APP_KEY"))
    webull_app_secret: str = field(default_factory=lambda: _env("WEBULL_APP_SECRET"))
    webull_region: str = field(default_factory=lambda: _env("WEBULL_REGION", "us"))
    webull_account_id: str = field(default_factory=lambda: _env("WEBULL_ACCOUNT_ID"))
    webull_paper: bool = field(default_factory=lambda: _env_bool("WEBULL_PAPER", True))

    # --- earnings calendar ---
    # Benzinga earnings is a separate paid Massive add-on (~$99/mo), so it is
    # opt-in even when a Massive key is present -- otherwise every request would
    # 403 and burn a retry.
    benzinga_earnings: bool = field(default_factory=lambda: _env_bool("BENZINGA_EARNINGS"))
    finnhub_token: str = field(default_factory=lambda: _env("FINNHUB_TOKEN"))
    # Nasdaq's calendar needs no key but is undocumented and unsupported.
    earnings_allow_nasdaq: bool = field(
        default_factory=lambda: _env_bool("EARNINGS_ALLOW_NASDAQ"))
    earnings_cache_seconds: float = field(
        default_factory=lambda: _env_float("EARNINGS_CACHE_SECONDS", 21600))

    # --- volatility history ---
    # Below this many stored observations, IV rank falls back to the
    # cross-sectional estimate and says so rather than reporting a rank built
    # from a handful of days.
    iv_rank_min_observations: int = field(
        default_factory=lambda: int(_env_float("IV_RANK_MIN_OBSERVATIONS", 30)))
    iv_rank_window_days: int = field(
        default_factory=lambda: int(_env_float("IV_RANK_WINDOW_DAYS", 365)))

    # --- pricing model ---
    risk_free_rate: float = field(default_factory=lambda: _env_float("RISK_FREE_RATE", 0.045))
    commission_per_contract: float = field(
        default_factory=lambda: _env_float("COMMISSION_PER_CONTRACT", 0.65))

    # --- caching (seconds) ---
    quote_cache_seconds: float = field(default_factory=lambda: _env_float("QUOTE_CACHE_SECONDS", 5))
    bars_cache_seconds: float = field(default_factory=lambda: _env_float("BARS_CACHE_SECONDS", 60))
    chain_cache_seconds: float = field(default_factory=lambda: _env_float("CHAIN_CACHE_SECONDS", 30))
    news_cache_seconds: float = field(default_factory=lambda: _env_float("NEWS_CACHE_SECONDS", 300))

    # --- database ---
    database_url: str = field(default_factory=lambda: _env(
        "DATABASE_URL", "postgresql+asyncpg://marketshark:marketshark@localhost:5432/marketshark"))

    # --- ai ---
    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "local"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    ai_model: str = field(default_factory=lambda: _env("AI_MODEL", "claude-opus-5"))
    # Comma-separated server allow-list. The browser can choose only from this
    # list; it cannot turn chat into an arbitrary paid-model proxy.
    ai_models: list[str] = field(default_factory=lambda: _env_list("AI_MODELS", []))

    # --- synthetic provider ---
    # Intraday drift on the synthetic QUOTE only. Bars stay byte-identical, so
    # backtests, forecasts and every test reproduce exactly; only the display
    # price moves. Without this a "live" UI polls forever and nothing ever
    # changes, which is worse than not claiming live at all.
    synthetic_live_ticks: bool = field(
        default_factory=lambda: _env("SYNTHETIC_LIVE_TICKS", "true").lower()
        not in ("0", "false", "no"))

    # --- http ---
    cors_origins: list[str] = field(default_factory=lambda: _env_list(
        "CORS_ORIGINS", ["http://localhost:3000", "http://127.0.0.1:3000"]))

    @property
    def has_live_data(self) -> bool:
        return bool(self.massive_api_key or (self.webull_app_key and self.webull_app_secret))

    @property
    def allows_synthetic_data(self) -> bool:
        """Whether generated data may be used as a development fallback."""
        return self.market_data_mode in {"demo", "test", "development"}

    @property
    def live_mode(self) -> bool:
        return not self.allows_synthetic_data

    @property
    def live_ready(self) -> bool:
        """Credentials are present; vendor entitlement is verified per request."""
        return self.live_mode and self.has_live_data

    def public_dict(self) -> dict:
        """Safe to send to the browser -- never includes secrets."""
        return {
            "environment": self.environment,
            "market_data_mode": "live" if self.live_mode else "demo",
            "has_live_data": self.has_live_data,
            "live_ready": self.live_ready,
            "providers_configured": {
                "massive": bool(self.massive_api_key),
                "webull": bool(self.webull_app_key and self.webull_app_secret),
                "synthetic": self.allows_synthetic_data,
            },
            "risk_free_rate": self.risk_free_rate,
            "commission_per_contract": self.commission_per_contract,
            "ai_enabled": bool(self.anthropic_api_key or self.openrouter_api_key or self.openai_api_key),
            "earnings_sources": {
                "benzinga": bool(self.massive_api_key and self.benzinga_earnings),
                "finnhub": bool(self.finnhub_token),
                "nasdaq": self.earnings_allow_nasdaq,
                "synthetic": self.allows_synthetic_data,
            },
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
