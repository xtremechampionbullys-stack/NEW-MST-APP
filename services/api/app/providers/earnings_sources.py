"""
Earnings calendar adapters.

Earnings dates are awkward to source: the good feeds are metered or paid, and
the free ones are undocumented. So there are several adapters and the registry
falls through them in order of quality.

  1. Massive / Benzinga  -- GET /benzinga/v1/earnings. The best source: confirmed
     vs projected status, BMO/AMC time, estimates and actuals, history to 2010.
     It is a paid add-on (~$99/mo) separate from the Stocks plan, so it is only
     used when explicitly enabled.
  2. Finnhub             -- GET /calendar/earnings. Free tier, 60 req/min, US
     coverage. Returns date, hour (bmo/amc/dmh), estimates and actuals.
  3. Nasdaq              -- api.nasdaq.com/api/calendar/earnings. No key at all,
     but undocumented and unsupported: it can change or disappear without
     notice, requires a browser-like User-Agent, and has no published rate
     limit. Off by default; opt in with EARNINGS_ALLOW_NASDAQ=true. Treat it as
     a convenience, not infrastructure.
  4. Synthetic           -- plausible quarterly dates so the feature is testable
     with no credentials at all. Always flagged is_synthetic=True.

What is deliberately not here: SEC EDGAR 8-K item 2.02. It is the authoritative
*historical* timestamp and worth adding for backtesting, but it cannot give a
forward date, which is what a strategy tester needs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import random

import httpx

from .base import BaseProvider, EarningsEvent, ProviderError, RateLimited

log = logging.getLogger(__name__)

_SESSION_MAP = {
    "bmo": "bmo", "before market open": "bmo", "pre-market": "bmo",
    "amc": "amc", "after market close": "amc", "after-hours": "amc",
    "dmh": "dmh", "during market hours": "dmh",
    "time-not-supplied": "unknown", "": "unknown",
}


def _session(raw: str | None) -> str:
    if not raw:
        return "unknown"
    key = str(raw).strip().lower()
    if key in _SESSION_MAP:
        return _SESSION_MAP[key]
    # Benzinga supplies a clock time in US/Eastern instead of a label.
    try:
        hour = int(key.split(":")[0])
        if hour < 9:
            return "bmo"
        if hour >= 16:
            return "amc"
        return "dmh"
    except (ValueError, IndexError):
        return "unknown"


def _f(value) -> float | None:
    if value in (None, "", "N/A", "--"):
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Benzinga via Massive
# --------------------------------------------------------------------------

class BenzingaEarningsProvider(BaseProvider):
    name = "benzinga"

    def __init__(self, api_key: str, *, base_url: str = "https://api.massive.com",
                 timeout: float = 15.0):
        if not api_key:
            raise ProviderError("Massive API key required", provider=self.name)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"})
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_earnings(self, symbol: str, *, lookback_quarters: int = 8,
                           lookahead_days: int = 120) -> list[EarningsEvent]:
        client = await self._http()
        start = dt.date.today() - dt.timedelta(days=int(lookback_quarters * 91.3))
        end = dt.date.today() + dt.timedelta(days=lookahead_days)

        resp = await client.get("/benzinga/v1/earnings", params={
            "ticker": symbol.upper(),
            "date.gte": start.isoformat(),
            "date.lte": end.isoformat(),
            "limit": 100,
            "sort": "date.asc",
        })
        if resp.status_code == 429:
            raise RateLimited("Benzinga earnings rate limit", provider=self.name)
        if resp.status_code in (401, 403):
            raise ProviderError(
                "Benzinga earnings is a paid Massive add-on (about $99/mo) and is "
                "separate from the Stocks plan. The key does not cover it.",
                provider=self.name)
        if resp.status_code >= 400:
            raise ProviderError(f"Benzinga earnings {resp.status_code}",
                                provider=self.name)

        out: list[EarningsEvent] = []
        for r in (resp.json().get("results") or []):
            day = r.get("date")
            if not day:
                continue
            try:
                event_date = dt.date.fromisoformat(day[:10])
            except ValueError:
                continue
            out.append(EarningsEvent(
                symbol=symbol.upper(), event_date=event_date,
                session=_session(r.get("time")),
                date_status=(r.get("date_status") or "projected").lower(),
                fiscal_period=r.get("fiscal_period"),
                fiscal_year=r.get("fiscal_year"),
                eps_estimate=_f(r.get("estimated_eps")),
                eps_actual=_f(r.get("actual_eps")),
                revenue_estimate=_f(r.get("estimated_revenue")),
                revenue_actual=_f(r.get("actual_revenue")),
                source=self.name,
            ))
        return out


# --------------------------------------------------------------------------
# Finnhub
# --------------------------------------------------------------------------

class FinnhubEarningsProvider(BaseProvider):
    name = "finnhub"

    def __init__(self, token: str, *, timeout: float = 15.0):
        if not token:
            raise ProviderError("Finnhub token required", provider=self.name)
        self.token = token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://finnhub.io/api/v1", timeout=self.timeout,
                headers={"X-Finnhub-Token": self.token})
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_earnings(self, symbol: str, *, lookback_quarters: int = 8,
                           lookahead_days: int = 120) -> list[EarningsEvent]:
        client = await self._http()
        start = dt.date.today() - dt.timedelta(days=int(lookback_quarters * 91.3))
        end = dt.date.today() + dt.timedelta(days=lookahead_days)

        resp = await client.get("/calendar/earnings", params={
            "symbol": symbol.upper(),
            "from": start.isoformat(),
            "to": end.isoformat(),
        })
        if resp.status_code == 429:
            raise RateLimited("Finnhub rate limit (60/min on the free tier)",
                              provider=self.name)
        if resp.status_code >= 400:
            raise ProviderError(f"Finnhub {resp.status_code}", provider=self.name)

        out: list[EarningsEvent] = []
        for r in (resp.json().get("earningsCalendar") or []):
            day = r.get("date")
            if not day:
                continue
            try:
                event_date = dt.date.fromisoformat(day[:10])
            except ValueError:
                continue
            # Finnhub does not distinguish confirmed from projected. Rather than
            # invent a status, mark anything with a reported actual as
            # confirmed-by-fact and leave the rest projected.
            has_actual = r.get("epsActual") is not None
            out.append(EarningsEvent(
                symbol=symbol.upper(), event_date=event_date,
                session=_session(r.get("hour")),
                date_status="confirmed" if has_actual else "projected",
                fiscal_period=f"Q{r['quarter']}" if r.get("quarter") else None,
                fiscal_year=r.get("year"),
                eps_estimate=_f(r.get("epsEstimate")),
                eps_actual=_f(r.get("epsActual")),
                revenue_estimate=_f(r.get("revenueEstimate")),
                revenue_actual=_f(r.get("revenueActual")),
                source=self.name,
            ))
        return out


# --------------------------------------------------------------------------
# Nasdaq (undocumented, opt-in)
# --------------------------------------------------------------------------

class NasdaqEarningsProvider(BaseProvider):
    """
    Nasdaq's public calendar JSON. No key required.

    Two caveats stated plainly, because this endpoint is not a supported
    product: it is date-indexed rather than symbol-indexed, so finding one
    company's next report means scanning forward day by day (we cap the scan and
    rate-limit ourselves), and it is undocumented, so it can change shape or
    vanish without warning. Enable it for convenience, not for anything you
    depend on.
    """

    name = "nasdaq"
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (compatible; MarketSharkTrader/0.1; "
                       "+https://marketsharktrader.com)"),
        "Accept": "application/json",
    }

    def __init__(self, *, timeout: float = 12.0, max_scan_days: int = 75):
        self.timeout = timeout
        self.max_scan_days = max_scan_days
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url="https://api.nasdaq.com", timeout=self.timeout,
                headers=self.HEADERS)
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_earnings(self, symbol: str, *, lookback_quarters: int = 8,
                           lookahead_days: int = 120) -> list[EarningsEvent]:
        client = await self._http()
        target = symbol.upper()
        found: list[EarningsEvent] = []
        scanned = 0
        day = dt.date.today()
        horizon = min(lookahead_days, self.max_scan_days)

        while scanned < horizon:
            if day.weekday() < 5:            # the calendar only has weekdays
                try:
                    resp = await client.get("/api/calendar/earnings",
                                            params={"date": day.isoformat()})
                except httpx.HTTPError as exc:
                    raise ProviderError(f"Nasdaq calendar unreachable: {exc}",
                                        provider=self.name, retryable=True) from exc
                if resp.status_code == 200:
                    rows = ((resp.json() or {}).get("data") or {}).get("rows") or []
                    for r in rows:
                        if (r.get("symbol") or "").upper() != target:
                            continue
                        found.append(EarningsEvent(
                            symbol=target, event_date=day,
                            session=_session(r.get("time")),
                            date_status="projected",
                            eps_estimate=_f(r.get("epsForecast")),
                            source=self.name,
                        ))
                    if found:
                        break                # first hit is the next report
                scanned += 1
                await asyncio.sleep(0.25)    # self-imposed; no published limit
            day += dt.timedelta(days=1)

        if not found:
            raise ProviderError(
                f"No earnings date found for {target} in the next {horizon} days",
                provider=self.name)
        return found


# --------------------------------------------------------------------------
# Synthetic
# --------------------------------------------------------------------------

# Index and sector ETFs do not report earnings. Giving them a synthetic
# announcement inflates their generated implied volatility and puts a bogus
# countdown on the dashboard -- and SPY/QQQ are in almost every watchlist, so
# the error would be the first thing anyone saw.
NON_REPORTING = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "EFA", "EEM", "TLT",
    "HYG", "LQD", "GLD", "SLV", "USO", "UNG", "XLF", "XLE", "XLK", "XLV",
    "XLI", "XLP", "XLU", "XLY", "XLB", "XLRE", "XLC", "SMH", "SOXX", "ARKK",
    "VXX", "UVXY", "SQQQ", "TQQQ", "SPX", "NDX", "RUT", "VIX",
}


def reports_earnings(symbol: str) -> bool:
    return symbol.upper() not in NON_REPORTING


def synthetic_earnings_profile(symbol: str) -> tuple[int, str, float]:
    """
    Deterministic (offset_days, session, jump_vol) for a symbol.

    Shared with the synthetic option chain so the generated volatility surface
    and the generated calendar describe the same company. If they disagreed, the
    term-structure decomposition would look broken on default data when in fact
    the inputs were inconsistent.
    """
    seed = int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    offset_days = rng.randint(18, 46)
    session = rng.choice(["bmo", "amc", "amc"])
    # One-off event return standard deviation: 3%-12%, the realistic band for
    # a large-cap earnings reaction.
    jump_vol = round(rng.uniform(0.03, 0.12), 4)
    return offset_days, session, jump_vol


def synthetic_earnings_dates(symbol: str, *, back: int = 8,
                             forward: int = 8) -> list[dt.date]:
    if not reports_earnings(symbol):
        return []
    offset_days, _, _ = synthetic_earnings_profile(symbol)
    today = dt.date.today()
    out: list[dt.date] = []
    for i in range(-back, forward):
        quarter_start = dt.date(today.year, 1, 1) + dt.timedelta(days=i * 91)
        day = quarter_start + dt.timedelta(days=offset_days)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        out.append(day)
    return sorted(out)


class SyntheticEarningsProvider(BaseProvider):
    """
    Deterministic quarterly dates so the earnings features are demonstrable and
    testable with no credentials. Every event is flagged is_synthetic so nothing
    downstream can mistake it for a real calendar.
    """

    name = "synthetic"

    async def get_earnings(self, symbol: str, *, lookback_quarters: int = 8,
                           lookahead_days: int = 120) -> list[EarningsEvent]:
        if not reports_earnings(symbol):
            raise ProviderError(
                f"{symbol.upper()} is an index or ETF and does not report earnings",
                provider=self.name)

        seed = int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        _, session, _ = synthetic_earnings_profile(symbol)
        today = dt.date.today()

        events: list[EarningsEvent] = []
        for day in synthetic_earnings_dates(symbol, back=lookback_quarters,
                                            forward=8):
            if day > today + dt.timedelta(days=lookahead_days):
                continue
            if day < today - dt.timedelta(days=int(lookback_quarters * 91.3)):
                continue

            is_past = day < today
            base_eps = round(rng.uniform(0.35, 3.2), 2)
            events.append(EarningsEvent(
                symbol=symbol.upper(), event_date=day, session=session,
                date_status="confirmed" if is_past or (day - today).days < 21
                            else "projected",
                fiscal_period=f"Q{((day.month - 1) // 3) + 1}",
                fiscal_year=day.year,
                eps_estimate=base_eps,
                eps_actual=round(base_eps * rng.uniform(0.88, 1.18), 2)
                if is_past else None,
                source=self.name, is_synthetic=True,
            ))
        return sorted(events, key=lambda e: e.event_date)
