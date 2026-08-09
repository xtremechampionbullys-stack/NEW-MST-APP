"""
TradingView integration: a UDF datafeed, and a straight account of broker wiring.

There are three different TradingView products people mean by "add TradingView
charts", and they have very different capabilities. Conflating them is how a
project ends up promising order entry it cannot deliver, so this module keeps
them apart and the `/status` endpoint says which one is actually live.

1. **Advanced Real-Time Chart widget** — a free `<script>` embed. Works today,
   no agreement, no key. It renders TradingView's own market data and its own
   symbols. It cannot display our data, and it cannot place orders. This is what
   the /charting page loads.

2. **Charting Library** — TradingView's self-hosted charting engine, free but
   distributed under an agreement you request from TradingView; the library
   files are not redistributable, so they are not in this repo. It renders
   *your* data through a datafeed adapter. The UDF endpoints below implement
   that protocol, so once the library is dropped in, pointing it at
   `/api/tv` is the whole integration. Still no order entry.

3. **Trading Platform + Broker Integration API** — the tier that puts a buy
   button on the chart. It requires the Charting Library agreement *and* an
   implementation of TradingView's broker REST contract, which TradingView
   reviews before enabling. Critically, it is the broker side that must be
   implemented and approved — you cannot reach a user's existing brokerage
   account through the widget, and a user connecting their broker inside
   tradingview.com does not expose that connection to this application.

What this file provides is (2): a working datafeed. `/api/tv/broker/status`
describes how far (3) has been mapped onto the order-preview code that already
exists, and is explicit that nothing is approved or connected.

Protocol reference: TradingView UDF (Universal Data Feed), the JSON contract
their `datafeeds/udf` adapter speaks.
"""

from __future__ import annotations

import datetime as dt
import time

from fastapi import APIRouter, HTTPException, Query

from ..providers.base import ProviderError
from ..providers.registry import get_registry
from ..services.market_map import SEGMENTS

router = APIRouter(prefix="/api/tv", tags=["charting"])

# UDF expects seconds-as-string resolutions for intraday and D/W/M for daily+.
SUPPORTED_RESOLUTIONS = ["1D", "1W", "1M"]

_EXCHANGE = "MARKETSHARK"


@router.get("/config")
async def config():
    """
    UDF `/config`. The Charting Library calls this first and adapts its UI to
    the answer, so every `false` here removes a control the user would otherwise
    click and find broken.
    """
    return {
        "supported_resolutions": SUPPORTED_RESOLUTIONS,
        "supports_group_request": False,
        "supports_marks": False,
        "supports_search": True,
        "supports_timescale_marks": False,
        # No streaming endpoint: the providers behind this are request/response
        # with a cache, so advertising realtime would make the library open a
        # subscription that never fires.
        "supports_time": True,
        "exchanges": [{"value": _EXCHANGE, "name": "MarketShark", "desc": "MarketShark Trader"}],
        "symbols_types": [{"name": "All", "value": ""},
                          {"name": "Stock", "value": "stock"},
                          {"name": "ETF", "value": "etf"}],
    }


@router.get("/time")
async def server_time():
    """UDF `/time`. Plain seconds since epoch, as the protocol requires."""
    return int(time.time())


@router.get("/symbols")
async def symbol_info(symbol: str = Query(..., alias="symbol")):
    """UDF `/symbols?symbol=`. Resolves one symbol to its trading metadata."""
    sym = symbol.split(":")[-1].upper()
    try:
        quote = await get_registry().quote(sym)
    except ProviderError as exc:
        raise HTTPException(404, f"unknown symbol {sym}: {exc}") from exc

    # The synthetic provider resolves ANY ticker -- it generates a deterministic
    # series from a seed, so there is no such thing as an unknown symbol and a
    # 404 is unreachable while it is the active provider. That is a real
    # property of the fallback, not something to paper over: a chart of a name
    # that does not exist, rendered without comment, is the worst possible
    # outcome here. So the substitution is pushed into the `description`, which
    # is the string the Charting Library prints in its symbol header and search
    # results -- the user sees it on the chart, not in a config endpoint.
    synthetic = getattr(quote, "source", "") == "synthetic"
    description = f"{sym} — SYNTHETIC (generated, not market data)" if synthetic else sym

    return {
        "name": sym,
        "ticker": sym,
        "description": description,
        "marketshark_synthetic": synthetic,
        "type": "stock",
        "session": "0930-1600",
        "timezone": "America/New_York",
        "exchange": _EXCHANGE,
        "listed_exchange": _EXCHANGE,
        "minmov": 1,
        "pricescale": 100,
        "has_intraday": False,
        "has_daily": True,
        "has_weekly_and_monthly": False,
        "supported_resolutions": SUPPORTED_RESOLUTIONS,
        "volume_precision": 0,
        "data_status": "endofday",
    }


@router.get("/search")
async def search(query: str = "", limit: int = Query(30, ge=1, le=100),
                 type: str = "", exchange: str = ""):
    """UDF `/search`. Matches against the instruments the board already knows."""
    known = sorted({inst.quote_symbol
                    for items in SEGMENTS.values() for inst in items}
                   | {"AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META", "COIN", "AMZN"})
    q = query.strip().upper()
    hits = [s for s in known if q in s] if q else known
    return [{"symbol": s, "full_name": f"{_EXCHANGE}:{s}", "description": s,
             "exchange": _EXCHANGE, "ticker": s, "type": "stock"}
            for s in hits[:limit]]


@router.get("/datafeed-note")
async def datafeed_note():
    """What a chart drawn from this feed is actually showing."""
    from ..providers.registry import get_registry as _reg
    preferred = _reg().preferred()
    return {
        "provider": preferred,
        "synthetic": preferred == "synthetic",
        "note": ("Every symbol resolves while the synthetic provider is active, "
                 "including tickers that do not exist — it generates a series "
                 "from a seed rather than looking anything up. Resolved symbols "
                 "carry SYNTHETIC in the description so it shows in the chart "
                 "header. Configure a data provider and this goes away."
                 if preferred == "synthetic" else
                 f"Charts are drawn from {preferred} data."),
    }


@router.get("/history")
async def history(symbol: str, resolution: str = "1D",
                  from_: int = Query(..., alias="from"),
                  to: int = Query(...), countback: int | None = None):
    """
    UDF `/history`. Bars in the column-oriented shape the protocol specifies.

    The `s: "no_data"` branch matters more than it looks: returning an empty
    `t` array instead makes the Charting Library retry the same range forever.
    `nextTime` tells it where data actually starts so it stops asking.
    """
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise HTTPException(400, f"unsupported resolution {resolution}; "
                                 f"this feed serves {SUPPORTED_RESOLUTIONS}")
    sym = symbol.split(":")[-1].upper()
    try:
        bars = await get_registry().bars(sym, limit=2000)
    except ProviderError as exc:
        raise HTTPException(503 if exc.provider == "configuration" else 502, str(exc)) from exc

    start = dt.datetime.fromtimestamp(from_, dt.timezone.utc)
    end = dt.datetime.fromtimestamp(to, dt.timezone.utc)
    window = [b for b in bars if start <= b.t <= end]

    if not window:
        earlier = [b for b in bars if b.t < start]
        return {"s": "no_data",
                "nextTime": int(earlier[-1].t.timestamp()) if earlier else None}

    return {
        "s": "ok",
        "t": [int(b.t.timestamp()) for b in window],
        "o": [b.o for b in window],
        "h": [b.h for b in window],
        "l": [b.l for b in window],
        "c": [b.c for b in window],
        "v": [b.v for b in window],
    }


@router.get("/status")
async def status():
    """What is wired, what is not, and what each tier would take."""
    return {
        "widget": {
            "tier": "Advanced Real-Time Chart widget",
            "live": True,
            "requires": "nothing — a script embed",
            "data": "TradingView's own market data",
            "trading": False,
            "note": ("What the Charting page loads today. It is TradingView's "
                     "chart on TradingView's data; this application's prices, "
                     "strategies and greeks are not in it."),
        },
        "charting_library": {
            "tier": "Charting Library (self-hosted)",
            "live": False,
            "requires": ("an agreement with TradingView; the library files are "
                         "not redistributable and so are not in this repo"),
            "data": "this API, via the UDF endpoints under /api/tv",
            "trading": False,
            "datafeed_ready": True,
            "datafeed_url": "/api/tv",
            "note": ("The datafeed half is done and testable without the "
                     "library — /api/tv/config, /symbols, /search, /history and "
                     "/time implement the UDF contract. Drop the library in and "
                     "point it at /api/tv."),
        },
        "broker_integration": {
            "tier": "Trading Platform + Broker Integration API",
            "live": False,
            "requires": ("the Charting Library agreement, an implementation of "
                         "TradingView's broker REST contract, and TradingView's "
                         "review before it is enabled"),
            "trading": True,
            "note": ("This is the tier that puts an order ticket on the chart, "
                     "and it is the most commonly misunderstood. The broker side "
                     "is what gets implemented and approved — there is no path "
                     "by which this application reaches a brokerage account "
                     "through the widget, and a user linking their broker inside "
                     "tradingview.com does not expose that link to us."),
            "mapped_from": {
                "accountsMetainfo": "/api/brokers/status",
                "preview": "/api/brokers/order/preview",
                "placeOrder": "/api/brokers/order/submit (disabled)",
                "positions": "/api/portfolio/positions",
            },
            "blocked_on": [
                "TradingView Charting Library agreement",
                "Webull order placement exercised against the sandbox",
                "auth and per-user account scoping",
            ],
        },
        "summary": ("Charts render today through the free widget. The UDF "
                    "datafeed is complete, so self-hosted charts on this API's "
                    "data are one agreement away. Order entry from the chart is "
                    "not, and nothing here should be read as though it were."),
    }
