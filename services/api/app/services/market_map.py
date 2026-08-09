"""
The market board: index / futures complex, sectors, and cross-asset.

A note on futures, because this is the place it would be easiest to lie.

The board is *shaped* like a futures screen — ES, NQ, RTY, CL, GC — because that
is the mental model traders have when they check "where is the market". But no
provider in this system currently serves futures: Massive's futures data is a
separate product from the equities plan, and Webull's futures entitlement is
region-gated. So each row carries both the instrument we would quote and the one
we actually did, and the response says which.

That distinction is not pedantry. ES and SPY track each other closely during the
session and diverge overnight, which is precisely when someone would open a
futures board — so an ETF proxy silently labelled "ES" would be most wrong at
exactly the moment it is most consulted. `instrument` and `proxy_for` make the
substitution visible on every row, and `is_proxy` lets the UI mark it.

When a futures-capable provider is configured, `FUTURES_SYMBOL` is already the
symbol to request; only the fetch needs changing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from dataclasses import dataclass, field

from ..providers.base import ProviderError
from ..providers.registry import get_registry


@dataclass
class Instrument:
    """One row of the board."""
    label: str
    quote_symbol: str               # what we can actually fetch today
    futures_symbol: str | None = None   # what we would fetch with a futures feed
    kind: str = "equity_index"
    note: str = ""

    @property
    def is_proxy(self) -> bool:
        return self.futures_symbol is not None


SEGMENTS: dict[str, list[Instrument]] = {
    "Index complex": [
        Instrument("S&P 500", "SPY", "ES", "equity_index"),
        Instrument("Nasdaq 100", "QQQ", "NQ", "equity_index"),
        Instrument("Russell 2000", "IWM", "RTY", "equity_index"),
        Instrument("Dow 30", "DIA", "YM", "equity_index"),
    ],
    "Sectors": [
        Instrument("Technology", "XLK", None, "sector"),
        Instrument("Financials", "XLF", None, "sector"),
        Instrument("Energy", "XLE", None, "sector"),
        Instrument("Health care", "XLV", None, "sector"),
        Instrument("Industrials", "XLI", None, "sector"),
        Instrument("Consumer disc.", "XLY", None, "sector"),
        Instrument("Semiconductors", "SMH", None, "sector"),
        Instrument("Utilities", "XLU", None, "sector"),
    ],
    "Rates, commodities, vol": [
        Instrument("20y+ Treasuries", "TLT", "ZB", "rates"),
        Instrument("Crude oil", "USO", "CL", "commodity"),
        Instrument("Gold", "GLD", "GC", "commodity"),
        Instrument("Volatility", "VXX", "VX", "volatility",
                   note="VXX holds front-month VIX futures and decays; it "
                        "tracks the direction of vol, not its level."),
    ],
}

BENCHMARK = "SPY"
TRADING_DAYS = 252.0


async def _row(inst: Instrument, days: int) -> dict:
    registry = get_registry()
    try:
        quote = await registry.quote(inst.quote_symbol)
        bars = await registry.bars(inst.quote_symbol, limit=days + 30)
    except ProviderError as exc:
        return {"label": inst.label, "symbol": inst.quote_symbol,
                "error": str(exc), "kind": inst.kind}

    closes = [b.c for b in bars]
    spark = closes[-60:]

    def change_over(n: int) -> float | None:
        if len(closes) <= n:
            return None
        prior = closes[-(n + 1)]
        return (closes[-1] / prior - 1.0) if prior else None

    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    tail = rets[-20:]
    realized = (float(sum(r * r for r in tail) / len(tail)) ** 0.5
                * math.sqrt(TRADING_DAYS)) if len(tail) >= 5 else None

    hi = max(closes[-252:]) if closes else None
    lo = min(closes[-252:]) if closes else None
    from_high = (closes[-1] / hi - 1.0) if hi else None

    return {
        "label": inst.label,
        "symbol": inst.quote_symbol,
        "kind": inst.kind,
        # The provenance fields. Present on every row, not only proxied ones,
        # so a consumer never has to infer meaning from a missing key.
        "instrument": "etf_proxy" if inst.is_proxy else "etf",
        "proxy_for": inst.futures_symbol,
        "is_proxy": inst.is_proxy,
        "last": quote.last,
        # NOTE ON UNITS, because this bit already bit once.
        #
        # `Quote.change_pct` is a PERCENTAGE NUMBER (-4.392 meaning -4.392%),
        # while every other ratio computed in this codebase is a FRACTION
        # (-0.04392). Mixing the two on one row produced a board reporting a
        # -439% day on an ETF, which is at least obviously wrong -- the
        # dangerous version is a 0.5% move rendered as 0.005% and read as calm.
        #
        # So the row never carries `change_pct`. Every window on it, including
        # the day, is a fraction, and they are named alike so nothing invites
        # the reader to assume one column is special.
        "change_1d": (quote.change_pct / 100.0
                      if quote.change_pct is not None else None),
        "change_1w": change_over(5),
        "change_1m": change_over(21),
        "change_3m": change_over(63),
        "realized_vol_20d": realized,
        "pct_from_52w_high": from_high,
        "range_52w": {"low": lo, "high": hi},
        "sparkline": spark,
        "source": quote.source,
        "note": inst.note,
    }


async def board(days: int = 90) -> dict:
    """The whole board, plus relative strength against the benchmark."""
    flat = [(seg, inst) for seg, items in SEGMENTS.items() for inst in items]
    rows = await asyncio.gather(*[_row(inst, days) for _, inst in flat])

    by_symbol = {r["symbol"]: r for r in rows}
    bench = by_symbol.get(BENCHMARK)

    segments = []
    for seg in SEGMENTS:
        members = [r for (s, _), r in zip(flat, rows) if s == seg]
        for r in members:
            # Relative strength is a difference of returns over the same window,
            # not a ratio of prices: a ratio would be dominated by whichever
            # instrument happened to have the larger price level.
            if bench and r.get("change_1m") is not None and bench.get("change_1m") is not None:
                r["rs_1m_vs_benchmark"] = r["change_1m"] - bench["change_1m"]
            else:
                r["rs_1m_vs_benchmark"] = None
        segments.append({"name": seg, "rows": members})

    ok = [r for r in rows if "error" not in r and r.get("change_1d") is not None]
    advancing = sum(1 for r in ok if r["change_1d"] > 0)

    proxied = [r for r in rows if r.get("is_proxy")]
    return {
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
        "benchmark": BENCHMARK,
        "segments": segments,
        "breadth": {
            "instruments": len(ok),
            "advancing": advancing,
            "declining": len(ok) - advancing,
            "advance_decline": (advancing / len(ok)) if ok else None,
        },
        "futures_available": False,
        "provenance": (
            f"{len(proxied)} of {len(rows)} rows are ETF proxies standing in for "
            f"futures contracts ({', '.join(r['proxy_for'] for r in proxied)}). "
            f"No configured provider serves futures, so those rows quote the ETF "
            f"and say so. Proxies track their contract closely during the cash "
            f"session and diverge overnight — which is exactly when a futures "
            f"board gets checked, so treat an out-of-hours reading as the ETF's "
            f"last print, not as the contract."
        ),
    }
