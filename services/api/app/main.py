"""MarketShark Trader API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db.engine import close_database, init_database
from .db.store import store_status
from .providers.registry import get_registry, reset_registry
from .routers import (
    agent_chat,
    agentics,
    algo,
    backtest,
    brokers,
    charting,
    dashboard,
    footprint,
    harness,
    intelligence,
    library,
    market,
    portfolio,
    research,
    strategy,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("marketshark")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    registry = get_registry()
    log.info("provider chain: %s", " -> ".join(registry.names))

    # The database is optional by design. init_database logs its own reason on
    # failure and the store layer falls back to memory, so a missing Postgres
    # costs persistence, not availability.
    await init_database()
    status = store_status()
    if status["persistent"]:
        log.info("store: %s (%s)", status["backend"], status["dialect"])
    else:
        log.warning(
            "store: in-memory. Saved strategies, positions and IV history will "
            "not survive a restart. Set DATABASE_URL to persist them. (%s)",
            status["detail"])

    if settings.live_mode and not settings.has_live_data:
        log.warning(
            "Live mode has no market-data credentials. Synthetic fallback is "
            "disabled; market endpoints will report setup required. Set "
            "MASSIVE_API_KEY or WEBULL_APP_KEY/SECRET for live data.")
    elif not settings.has_live_data:
        log.warning("Demo mode is active: generated data is enabled. Set "
                    "MARKET_DATA_MODE=live and configure a provider before using "
                    "the application for market decisions.")
    yield
    await reset_registry()
    await close_database()


app = FastAPI(
    title="MarketShark Trader API",
    version="0.1.0",
    description=(
        "Options strategy testing, analytics, and market data for "
        "marketsharktrader.com."
    ),
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _traffic(request, call_next):
    """
    Latency recording for every API call, OpenAlgo-style.

    Grouping is by route template, not raw path — /api/market/quote/NVDA and
    /quote/AAPL are one endpoint, and per-symbol buckets would make every
    percentile a sample of one.
    """
    import time as _time                                           # noqa: PLC0415
    from .services import algo_gateway as _gw                      # noqa: PLC0415
    start = _time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        _gw.traffic.record(path=path, method=request.method,
                           status=response.status_code,
                           ms=(_time.perf_counter() - start) * 1000)
    return response


app.include_router(market.router)
app.include_router(strategy.router)
app.include_router(research.router)
app.include_router(brokers.router)
app.include_router(portfolio.router)
app.include_router(portfolio.agents_router)
app.include_router(library.router)
app.include_router(library.earnings_router)
app.include_router(library.vol_router)
app.include_router(backtest.router)
app.include_router(dashboard.router)
app.include_router(intelligence.forecast_router)
app.include_router(intelligence.forward_router)
app.include_router(intelligence.dojo_router)
app.include_router(charting.router)
app.include_router(harness.router)
app.include_router(agent_chat.router)
app.include_router(agentics.router)
app.include_router(algo.router)
app.include_router(footprint.router)


@app.get("/api/health")
async def health():
    registry = get_registry()
    return {
        "status": "ok",
        "version": app.version,
        "providers": registry.status(),
        "earnings_providers": registry.earnings_status(),
        "preferred_provider": registry.preferred(),
        "store": store_status(),
        "config": get_settings().public_dict(),
    }


@app.get("/")
async def root():
    return {
        "name": "MarketShark Trader API",
        "docs": "/docs",
        "health": "/api/health",
    }
