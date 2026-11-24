"""
Massive (formerly Polygon.io) adapter.

Polygon rebranded to Massive in October 2025. api.polygon.io still resolves,
but the documented host is api.massive.com and the Python package was renamed
polygon-api-client -> massive. We talk to the REST API over httpx directly
rather than depending on the vendor SDK: one less dependency to keep in step
with a rebrand, and full control over retry and rate-limit behaviour.

Plan gating that matters here
-----------------------------
* Basic (free): 5 requests/minute, no option chain snapshots, no greeks or IV.
  The adapter detects this and raises NotEntitled so the registry can fall back
  rather than silently rendering an empty chain.
* Starter and above: chain snapshots with greeks and IV, 15-minute delayed.
* Advanced and above: real-time.

Endpoints used
--------------
  GET /v3/snapshot/options/{underlying}     chain with greeks + IV
  GET /v2/aggs/ticker/{ticker}/range/...    bars (stocks and options)
  GET /v2/last/trade/{ticker}               last trade
  GET /v3/reference/tickers/{ticker}        company profile
  GET /stocks/financials/v1/income-statements    fundamentals (v1 replaced /vX)
  GET /stocks/financials/v1/balance-sheets
  GET /v2/reference/news                    news with sentiment insights
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import httpx

from .base import (
    Bar,
    BaseProvider,
    CompanyProfile,
    Fundamentals,
    NewsItem,
    NotEntitled,
    OptionChain,
    OptionContract,
    ProviderError,
    Quote,
    RateLimited,
    parse_occ,
)

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.massive.com"


class MassiveProvider(BaseProvider):
    name = "massive"
    supports_greeks = True

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE,
                 timeout: float = 15.0, is_realtime: bool = False):
        if not api_key:
            raise ProviderError("Massive API key is required", provider=self.name)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.is_realtime = is_realtime
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                # Header auth rather than ?apiKey= : the query form leaks the key
                # into logs, and next_url pagination values come back without it.
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Accept": "application/json"},
            )
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None,
                   *, retries: int = 2) -> dict:
        client = await self._http()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_exc = exc
                await asyncio.sleep(0.4 * (attempt + 1))
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 12))
                if attempt < retries:
                    await asyncio.sleep(min(retry_after, 15))
                    continue
                raise RateLimited(
                    "Massive rate limit hit. The free tier allows 5 requests per "
                    "minute; a paid options plan removes this.",
                    provider=self.name, retry_after=retry_after)

            if resp.status_code in (401, 403):
                raise NotEntitled(
                    f"Massive rejected the request ({resp.status_code}). The "
                    "options chain snapshot and greeks require an Options "
                    "Starter plan or above.", provider=self.name)

            if resp.status_code == 404:
                raise ProviderError(f"Massive: not found ({path})", provider=self.name)

            if resp.status_code >= 500:
                last_exc = ProviderError(f"Massive {resp.status_code}",
                                         provider=self.name, retryable=True)
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

            resp.raise_for_status()
            return resp.json()

        raise ProviderError(f"Massive request failed: {last_exc}",
                            provider=self.name, retryable=True)

    async def is_available(self) -> bool:
        try:
            await self._get("/v3/reference/tickers/AAPL")
            return True
        except ProviderError:
            return False

    # ---- quotes and bars -------------------------------------------------

    async def get_quote(self, symbol: str) -> Quote:
        sym = symbol.upper()
        prev = await self._get(f"/v2/aggs/ticker/{sym}/prev", {"adjusted": "true"})
        results = prev.get("results") or []
        if not results:
            raise ProviderError(f"Massive returned no data for {sym}",
                                provider=self.name)
        bar = results[0]

        last_price = bar.get("c")
        try:
            trade = await self._get(f"/v2/last/trade/{sym}")
            last_price = (trade.get("results") or {}).get("p", last_price)
        except ProviderError:
            pass   # last-trade is a paid endpoint on some plans; prev close is fine

        prev_close = bar.get("c")
        return Quote(
            symbol=sym, last=float(last_price), open=bar.get("o"),
            high=bar.get("h"), low=bar.get("l"), prev_close=prev_close,
            volume=bar.get("v"),
            change=(float(last_price) - prev_close) if prev_close else None,
            change_pct=((float(last_price) / prev_close - 1) * 100)
            if prev_close else None,
            timestamp=dt.datetime.now(dt.timezone.utc),
            delayed=not self.is_realtime, source=self.name,
        )

    async def get_bars(self, symbol: str, *, timespan: str = "day",
                       limit: int = 250) -> list[Bar]:
        span_days = {"minute": 5, "hour": 40, "day": int(limit * 1.6) + 10,
                     "week": limit * 8, "month": limit * 32}.get(timespan, limit * 2)
        end = dt.date.today()
        start = end - dt.timedelta(days=max(span_days, 7))
        data = await self._get(
            f"/v2/aggs/ticker/{symbol.upper()}/range/1/{timespan}/{start}/{end}",
            {"adjusted": "true", "sort": "asc", "limit": 50000})
        return [
            Bar(t=dt.datetime.fromtimestamp(r["t"] / 1000, dt.timezone.utc),
                o=r["o"], h=r["h"], l=r["l"], c=r["c"], v=r.get("v", 0.0),
                n=int(r["n"]) if r.get("n") is not None else None,
                source=self.name)
            for r in (data.get("results") or [])
        ][-limit:]

    # ---- option chain ----------------------------------------------------

    async def get_option_chain(self, symbol: str, *,
                               expiration: dt.date | None = None,
                               max_expirations: int = 6,
                               strike_window_pct: float = 0.30) -> OptionChain:
        sym = symbol.upper()
        quote = await self.get_quote(sym)
        spot = quote.last

        params: dict = {"limit": 250}
        if expiration:
            params["expiration_date"] = expiration.isoformat()
        else:
            # Comparator suffixes are supported on expiration_date and strike_price.
            params["expiration_date.gte"] = dt.date.today().isoformat()
        params["strike_price.gte"] = round(spot * (1 - strike_window_pct), 2)
        params["strike_price.lte"] = round(spot * (1 + strike_window_pct), 2)

        contracts: list[OptionContract] = []
        seen_expirations: set[dt.date] = set()
        path = f"/v3/snapshot/options/{sym}"
        pages = 0

        while path and pages < 12:
            data = await self._get(path, params if pages == 0 else None)
            for r in data.get("results") or []:
                details = r.get("details") or {}
                greeks = r.get("greeks") or {}
                quote_blk = r.get("last_quote") or {}
                trade_blk = r.get("last_trade") or {}
                day_blk = r.get("day") or {}

                ticker = details.get("ticker")
                if not ticker:
                    continue
                try:
                    _, exp, right, strike = parse_occ(ticker)
                except ValueError:
                    continue

                if expiration is None and exp not in seen_expirations:
                    if len(seen_expirations) >= max_expirations:
                        continue
                    seen_expirations.add(exp)

                contracts.append(OptionContract(
                    symbol=ticker, underlying=sym, expiration=exp,
                    strike=float(details.get("strike_price", strike)),
                    right="call" if details.get("contract_type") == "call" else right,
                    bid=quote_blk.get("bid"), ask=quote_blk.get("ask"),
                    last=trade_blk.get("price"),
                    volume=day_blk.get("volume"),
                    open_interest=r.get("open_interest"),
                    implied_vol=r.get("implied_volatility"),
                    delta=greeks.get("delta"), gamma=greeks.get("gamma"),
                    theta=greeks.get("theta"), vega=greeks.get("vega"),
                    # Massive's snapshot carries no rho -- left None rather than
                    # fabricated; the engine computes it locally when needed.
                    rho=None,
                    multiplier=float(details.get("shares_per_contract", 100)),
                    exercise_style=details.get("exercise_style", "american"),
                    source=self.name,
                ))

            next_url = data.get("next_url")
            if not next_url:
                break
            path = next_url.replace(self.base_url, "")
            pages += 1

        if not contracts:
            raise NotEntitled(
                "Massive returned an empty option chain. Chain snapshots require "
                "an Options Starter plan or above; the free tier cannot serve them.",
                provider=self.name)

        return OptionChain(underlying=sym, spot=spot, contracts=contracts,
                           source=self.name)

    # ---- reference data --------------------------------------------------

    async def get_profile(self, symbol: str) -> CompanyProfile:
        data = await self._get(f"/v3/reference/tickers/{symbol.upper()}")
        r = data.get("results") or {}
        branding = r.get("branding") or {}
        return CompanyProfile(
            ticker=r.get("ticker", symbol.upper()), name=r.get("name", ""),
            description=r.get("description", ""),
            sector=r.get("sic_description", ""),
            industry=r.get("sic_description", ""),
            exchange=r.get("primary_exchange", ""),
            market_cap=r.get("market_cap"),
            shares_outstanding=r.get("weighted_shares_outstanding")
            or r.get("share_class_shares_outstanding"),
            employees=r.get("total_employees"),
            homepage=r.get("homepage_url", ""),
            logo_url=branding.get("logo_url"),
            list_date=r.get("list_date"),
        )

    async def get_fundamentals(self, symbol: str, *, timeframe: str = "quarterly",
                               limit: int = 8) -> list[Fundamentals]:
        """
        The old /vX/reference/financials tree was replaced by flat, versioned
        v1 statement endpoints. We pull income and balance sheet and join them
        on period_end.
        """
        sym = symbol.upper()
        params = {"tickers": sym, "timeframe": timeframe, "limit": limit,
                  "sort": "period_end", "order": "desc"}

        income = (await self._get("/stocks/financials/v1/income-statements",
                                  params)).get("results") or []
        try:
            balance = (await self._get("/stocks/financials/v1/balance-sheets",
                                       params)).get("results") or []
        except ProviderError:
            balance = []
        by_period = {b.get("period_end"): b for b in balance}

        out: list[Fundamentals] = []
        for row in income:
            period = row.get("period_end", "")
            b = by_period.get(period, {})
            revenue = row.get("revenue")
            net_income = row.get("net_income_loss_attributable_common_shareholders")
            equity = b.get("total_equity") or b.get("equity")
            liabilities = b.get("total_liabilities")

            out.append(Fundamentals(
                ticker=sym, period_end=period, timeframe=timeframe,
                revenue=revenue, gross_profit=row.get("gross_profit"),
                operating_income=row.get("operating_income"),
                net_income=net_income, ebitda=row.get("ebitda"),
                eps_basic=row.get("basic_earnings_per_share"),
                eps_diluted=row.get("diluted_earnings_per_share"),
                total_assets=b.get("total_assets"),
                total_liabilities=liabilities, equity=equity,
                gross_margin=_ratio(row.get("gross_profit"), revenue),
                operating_margin=_ratio(row.get("operating_income"), revenue),
                net_margin=_ratio(net_income, revenue),
                roe=_ratio(net_income, equity),
                debt_to_equity=_ratio(liabilities, equity),
            ))
        return out

    async def get_news(self, symbols: list[str], *, limit: int = 25) -> list[NewsItem]:
        items: list[NewsItem] = []
        per = max(1, limit // max(len(symbols), 1))
        for sym in symbols or ["SPY"]:
            data = await self._get("/v2/reference/news",
                                   {"ticker": sym.upper(), "limit": per,
                                    "order": "desc", "sort": "published_utc"})
            for r in data.get("results") or []:
                insights = r.get("insights") or []
                mine = next((i for i in insights
                             if i.get("ticker", "").upper() == sym.upper()), None)
                publisher = (r.get("publisher") or {}).get("name", "")
                items.append(NewsItem(
                    id=r.get("id", r.get("article_url", "")),
                    title=r.get("title", ""), url=r.get("article_url", ""),
                    published=_parse_ts(r.get("published_utc")),
                    publisher=publisher, author=r.get("author", ""),
                    summary=r.get("description", ""),
                    image_url=r.get("image_url"),
                    tickers=[t.upper() for t in (r.get("tickers") or [])],
                    sentiment=(mine or {}).get("sentiment"),
                    sentiment_reasoning=(mine or {}).get("sentiment_reasoning"),
                    media_type="article",
                ))
        items.sort(key=lambda i: i.published, reverse=True)
        return items[:limit]


def _ratio(num, den):
    try:
        if num is None or not den:
            return None
        return round(float(num) / float(den), 6)
    except (TypeError, ZeroDivisionError, ValueError):
        return None


def _parse_ts(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)
