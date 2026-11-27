"""
Market data provider interface.

Every adapter -- Massive (formerly Polygon), Webull OpenAPI, and the synthetic
generator -- implements this. The rest of the system never imports a vendor SDK
directly, so swapping or adding a provider is a single file plus a registry
entry, and the strategy tester behaves identically whichever one is live.

Design notes
------------
* All methods are async. Real adapters do network I/O; the synthetic one is
  trivially async so callers do not branch.
* Every method may raise ProviderError. Callers are expected to degrade rather
  than fail: the registry falls back down a chain, so a missing API key or an
  exhausted rate limit costs you live data, not a working page.
* Timestamps are UTC. Prices are floats in the quote currency.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Literal, Protocol, runtime_checkable

OptionRight = Literal["call", "put"]


class ProviderError(RuntimeError):
    """Any provider-side failure: auth, rate limit, entitlement, bad symbol."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class NotEntitled(ProviderError):
    """The account's plan does not cover this data (e.g. Massive free tier)."""


class RateLimited(ProviderError):
    def __init__(self, message: str, *, provider: str = "", retry_after: float = 1.0):
        super().__init__(message, provider=provider, retryable=True)
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------

@dataclass
class Quote:
    symbol: str
    last: float
    bid: float | None = None
    ask: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: float | None = None
    change: float | None = None
    change_pct: float | None = None
    timestamp: dt.datetime | None = None
    delayed: bool = True
    source: str = ""

    @property
    def mid(self) -> float:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat() if self.timestamp else None
        d["mid"] = self.mid
        return d


@dataclass
class Bar:
    t: dt.datetime
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

    def to_dict(self) -> dict:
        return {"t": int(self.t.timestamp() * 1000), "o": self.o, "h": self.h,
                "l": self.l, "c": self.c, "v": self.v}


@dataclass
class OptionContract:
    symbol: str                    # OCC-style contract symbol
    underlying: str
    expiration: dt.date
    strike: float
    right: OptionRight
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    implied_vol: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    multiplier: float = 100.0
    exercise_style: str = "american"
    source: str = ""

    @property
    def dte(self) -> int:
        return max((self.expiration - dt.date.today()).days, 0)

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.ask >= self.bid >= 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        m, s = self.mid, self.spread
        if not m or s is None or m <= 0:
            return None
        return s / m

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiration"] = self.expiration.isoformat()
        d["dte"] = self.dte
        d["mid"] = self.mid
        d["spread"] = self.spread
        d["spread_pct"] = self.spread_pct
        return d


@dataclass
class OptionChain:
    underlying: str
    spot: float
    contracts: list[OptionContract] = field(default_factory=list)
    fetched_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    source: str = ""

    def expirations(self) -> list[dt.date]:
        return sorted({c.expiration for c in self.contracts})

    def strikes(self, expiration: dt.date | None = None) -> list[float]:
        pool = self.contracts if expiration is None else \
            [c for c in self.contracts if c.expiration == expiration]
        return sorted({c.strike for c in pool})

    def for_expiration(self, expiration: dt.date) -> list[OptionContract]:
        return sorted((c for c in self.contracts if c.expiration == expiration),
                      key=lambda c: (c.strike, c.right))

    def find(self, expiration: dt.date, strike: float,
             right: OptionRight) -> OptionContract | None:
        for c in self.contracts:
            if (c.expiration == expiration and abs(c.strike - strike) < 1e-6
                    and c.right == right):
                return c
        return None

    def nearest_expiration(self, target_dte: int) -> dt.date | None:
        exps = self.expirations()
        if not exps:
            return None
        today = dt.date.today()
        return min(exps, key=lambda e: abs((e - today).days - target_dte))

    def to_dict(self, expiration: dt.date | None = None) -> dict:
        pool = self.contracts if expiration is None else self.for_expiration(expiration)
        return {
            "underlying": self.underlying,
            "spot": self.spot,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
            "expirations": [e.isoformat() for e in self.expirations()],
            "contracts": [c.to_dict() for c in pool],
        }


@dataclass
class NewsItem:
    id: str
    title: str
    url: str
    published: dt.datetime
    publisher: str = ""
    author: str = ""
    summary: str = ""
    image_url: str | None = None
    tickers: list[str] = field(default_factory=list)
    sentiment: str | None = None
    sentiment_reasoning: str | None = None
    media_type: Literal["article", "video"] = "article"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat()
        return d


@dataclass
class EarningsEvent:
    """
    A scheduled or historical earnings announcement.

    `date_status` preserves whether the company has confirmed the date or a
    vendor has projected it. That distinction is load-bearing: a projected date
    can move by a week, and sizing a trade around an expiry that "avoids
    earnings" on a projected date is a false sense of safety.

    `session` is bmo / amc / dmh (before open, after close, during hours). It
    decides which trading session the reaction lands in, which is the difference
    between a position surviving the event and not.
    """
    symbol: str
    event_date: dt.date
    session: str = "unknown"
    date_status: str = "projected"
    fiscal_period: str | None = None
    fiscal_year: int | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    source: str = ""
    is_synthetic: bool = False

    @property
    def days_away(self) -> int:
        return (self.event_date - dt.date.today()).days

    @property
    def is_upcoming(self) -> bool:
        return self.event_date >= dt.date.today()

    @property
    def is_confirmed(self) -> bool:
        return self.date_status.lower() == "confirmed"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_date"] = self.event_date.isoformat()
        d["days_away"] = self.days_away
        d["is_upcoming"] = self.is_upcoming
        d["is_confirmed"] = self.is_confirmed
        return d


@dataclass
class CompanyProfile:
    ticker: str
    name: str = ""
    description: str = ""
    sector: str = ""
    industry: str = ""
    exchange: str = ""
    market_cap: float | None = None
    shares_outstanding: float | None = None
    employees: int | None = None
    homepage: str = ""
    logo_url: str | None = None
    list_date: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Fundamentals:
    """
    A normalized financial snapshot. Field names are provider-agnostic; each
    adapter maps its own vocabulary onto these.
    """
    ticker: str
    period_end: str = ""
    timeframe: str = "annual"
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    ebitda: float | None = None
    eps_basic: float | None = None
    eps_diluted: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    equity: float | None = None
    cash: float | None = None
    debt: float | None = None
    free_cash_flow: float | None = None
    operating_cash_flow: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    roe: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------

@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    async def is_available(self) -> bool: ...

    async def get_quote(self, symbol: str) -> Quote: ...

    async def get_bars(self, symbol: str, *, timespan: str = "day",
                       limit: int = 250) -> list[Bar]: ...

    async def get_option_chain(self, symbol: str, *,
                               expiration: dt.date | None = None,
                               max_expirations: int = 6,
                               strike_window_pct: float = 0.30) -> OptionChain: ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    async def get_profile(self, symbol: str) -> CompanyProfile: ...
    async def get_fundamentals(self, symbol: str, *, timeframe: str = "quarterly",
                               limit: int = 8) -> list[Fundamentals]: ...


@runtime_checkable
class NewsProvider(Protocol):
    async def get_news(self, symbols: list[str], *, limit: int = 25) -> list[NewsItem]: ...


@runtime_checkable
class EarningsProvider(Protocol):
    async def get_earnings(self, symbol: str, *, lookback_quarters: int = 8,
                           lookahead_days: int = 120) -> list[EarningsEvent]: ...


class BaseProvider:
    """Shared helpers. Adapters subclass this and override what they support."""

    name: str = "base"
    supports_greeks: bool = False
    is_realtime: bool = False

    async def is_available(self) -> bool:
        return True

    def _unsupported(self, what: str):
        raise ProviderError(f"{self.name} does not support {what}", provider=self.name)

    async def get_quote(self, symbol: str) -> Quote:
        self._unsupported("quotes")

    async def get_bars(self, symbol: str, *, timespan: str = "day",
                       limit: int = 250) -> list[Bar]:
        self._unsupported("bars")

    async def get_option_chain(self, symbol: str, *, expiration=None,
                               max_expirations: int = 6,
                               strike_window_pct: float = 0.30) -> OptionChain:
        self._unsupported("option chains")

    async def get_profile(self, symbol: str) -> CompanyProfile:
        self._unsupported("company profiles")

    async def get_fundamentals(self, symbol: str, *, timeframe: str = "quarterly",
                               limit: int = 8) -> list[Fundamentals]:
        self._unsupported("fundamentals")

    async def get_news(self, symbols: list[str], *, limit: int = 25) -> list[NewsItem]:
        self._unsupported("news")

    async def get_earnings(self, symbol: str, *, lookback_quarters: int = 8,
                           lookahead_days: int = 120) -> list[EarningsEvent]:
        self._unsupported("earnings calendars")

    async def get_trades(self, symbol: str, *, sessions: int = 1):
        """Individual trades for the last `sessions` sessions, as a TradeTape
        (app.engine.footprint). Tick data is a separate entitlement at every
        vendor, so most adapters do not have it."""
        self._unsupported("tick-level trades")


# --------------------------------------------------------------------------
# OCC symbol helpers -- shared by every adapter
# --------------------------------------------------------------------------

def build_occ(root: str, expiration: dt.date, right: OptionRight,
              strike: float) -> str:
    """
    OCC contract symbol: ROOT + YYMMDD + C|P + strike x 1000, 8 digits.
    e.g. AAPL251219C00250000
    """
    return (f"{root.upper()}{expiration:%y%m%d}"
            f"{'C' if right == 'call' else 'P'}"
            f"{int(round(strike * 1000)):08d}")


def parse_occ(symbol: str) -> tuple[str, dt.date, OptionRight, float]:
    """Inverse of build_occ. Accepts an optional Massive-style 'O:' prefix."""
    s = symbol.strip().upper()
    if s.startswith("O:"):
        s = s[2:]
    if len(s) < 15:
        raise ValueError(f"not an OCC option symbol: {symbol}")
    strike = int(s[-8:]) / 1000.0
    right: OptionRight = "call" if s[-9] == "C" else "put"
    date_part = s[-15:-9]
    root = s[:-15]
    expiration = dt.date(2000 + int(date_part[0:2]), int(date_part[2:4]),
                         int(date_part[4:6]))
    return root, expiration, right, strike
