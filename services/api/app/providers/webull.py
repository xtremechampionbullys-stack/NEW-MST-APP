"""
Webull OpenAPI adapter.

Two things about this API are easy to get wrong and worth stating up front:

1. The SDK generation changed. The `webull-python-sdk-*` family (importing as
   webullsdkcore / webullsdkmdata / webullsdktrade) was archived in June 2026
   and replaced by `webull-openapi-python-sdk`, which imports as `webull`. We
   implement the HMAC signing ourselves against httpx instead of taking the SDK
   dependency, because the signing scheme is small, the SDK pulls in grpcio and
   paho-mqtt for features we do not use here, and this keeps the adapter honest
   about exactly what goes over the wire.

2. `v2` is a *header* (`x-version`), not a URL path segment. Inserting `/v2/`
   into the path produces a signature mismatch, not a 404, which is a
   thoroughly confusing way to fail.

Signing scheme
--------------
  body_hash    = SHA256(compact_json(body)).hexdigest().upper()   (or "None")
  sign_params  = lowercased signing headers + query params + Host
  string       = uri + "&" + "&".join(sorted("k=v")) + "&" + body_hash
  signature    = base64(HMAC_SHA256(urlencode(string, safe=""), secret + "&"))

Note the trailing "&" appended to the secret, and that the whole string is URL
encoded with safe='' so even slashes become %2F.

Greeks caveat
-------------
Webull's option snapshot is documented as returning price, change, volume,
turnover and best bid/ask. Greeks and IV are *not* documented as included. We
parse them opportunistically and, when they are absent, leave them None -- the
engine then computes IV and greeks locally from the mid price, which is the
correct fallback rather than shipping blanks to the UI.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import uuid
from urllib.parse import quote, urlparse

import httpx

from ..engine.pricing import bs_greeks, implied_vol, years_from_dte
from .base import (
    Bar,
    BaseProvider,
    OptionChain,
    OptionContract,
    ProviderError,
    Quote,
    RateLimited,
    build_occ,
)

log = logging.getLogger(__name__)

REGION_HOSTS = {
    "us": "api.webull.com",
    "hk": "api.webull.hk",
    "jp": "api.webull.co.jp",
    "us-sandbox": "api.sandbox.webull.com",
}

SIGN_ALGORITHM = "HMAC-SHA256"
SIGN_VERSION = "1.0"
API_VERSION = "v2"

# Per-endpoint limits are tighter than the headline 600/min: option snapshot
# and tick are 60/min, and option historical bars is 1 call/sec per app key.
OPTION_SNAPSHOT_BATCH = 20


class WebullProvider(BaseProvider):
    name = "webull"
    supports_greeks = False          # not documented; computed locally instead
    is_realtime = True

    def __init__(self, app_key: str, app_secret: str, *, region: str = "us",
                 timeout: float = 15.0, rate: float = 0.045):
        if not app_key or not app_secret:
            raise ProviderError("Webull app key and secret are required",
                                provider=self.name)
        self.app_key = app_key
        self.app_secret = app_secret
        self.region = region
        self.host = REGION_HOSTS.get(region, REGION_HOSTS["us"])
        self.timeout = timeout
        self.rate = rate
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ---- signing ---------------------------------------------------------

    def _sign(self, uri: str, params: dict | None, body: dict | None) -> dict:
        nonce = uuid.uuid4().hex
        timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")

        headers = {
            "x-app-key": self.app_key,
            "x-signature-algorithm": SIGN_ALGORITHM,
            "x-signature-version": SIGN_VERSION,
            "x-signature-nonce": nonce,
            "x-timestamp": timestamp,
            "x-version": API_VERSION,
            "Content-Type": "application/json",
        }

        if body is None:
            body_hash = "None"
        else:
            compact = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            body_hash = hashlib.sha256(compact.encode()).hexdigest().upper()

        sign_params = {
            "x-app-key": self.app_key,
            "x-timestamp": timestamp,
            "x-signature-version": SIGN_VERSION,
            "x-signature-algorithm": SIGN_ALGORITHM,
            "x-signature-nonce": nonce,
            "Host": self.host,          # signed, but must not be sent as a header
        }
        for k, v in (params or {}).items():
            sign_params[k] = str(v)

        joined = "&".join(f"{k}={sign_params[k]}" for k in sorted(sign_params))
        string_to_sign = f"{uri}&{joined}&{body_hash}"
        encoded = quote(string_to_sign, safe="")

        digest = hmac.new((self.app_secret + "&").encode(), encoded.encode(),
                          hashlib.sha256).digest()
        headers["x-signature"] = base64.b64encode(digest).decode()
        return headers

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=f"https://{self.host}",
                                             timeout=self.timeout)
        return self._client

    async def aclose(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, uri: str, *, params: dict | None = None,
                       body: dict | None = None, retries: int = 2) -> dict:
        client = await self._http()
        for attempt in range(retries + 1):
            headers = self._sign(uri, params, body)
            try:
                resp = await client.request(method, uri, params=params,
                                            json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise ProviderError(f"Webull transport error: {exc}",
                                    provider=self.name, retryable=True)

            if resp.status_code == 429:
                if attempt < retries:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise RateLimited(
                    "Webull rate limit. Option snapshot and tick are capped at "
                    "60 requests/minute and option bars at 1/second per app key.",
                    provider=self.name, retry_after=2.0)

            if resp.status_code in (401, 403):
                raise ProviderError(
                    "Webull rejected the signature or entitlement. Check the app "
                    "key/secret pair, the region, and that market data is "
                    "subscribed -- OpenAPI data is purchased separately from "
                    "API access.", provider=self.name)

            if resp.status_code >= 400:
                raise ProviderError(
                    f"Webull {resp.status_code}: {resp.text[:300]}",
                    provider=self.name, retryable=resp.status_code >= 500)

            return resp.json()

        raise ProviderError("Webull request failed", provider=self.name,
                            retryable=True)

    async def is_available(self) -> bool:
        try:
            await self._request("GET", "/openapi/market-data/stock/snapshot",
                                params={"symbols": "AAPL", "category": "US_STOCK"})
            return True
        except ProviderError:
            return False

    # ---- accounts --------------------------------------------------------
    #
    # URIs verified against the official `webull-openapi-python-sdk` source and
    # developer.webull.com reference pages (2026-08), not recalled: account list
    # is /openapi/account/list, while balance and positions moved to an
    # /openapi/assets/ prefix in the v2 API the US docs demo with. The JP docs
    # still show /openapi/account/balance — that is the older wrapper, and
    # sending it to the US host is the kind of mistake that produces a
    # convincing 404 and a "Webull is down" diagnosis.
    #
    # Response schemas are published in full only for the JP region, so parsing
    # is defensive: absent fields become None rather than 0.0, because "no cash
    # figure reported" and "zero cash" are different facts about an account.

    async def get_accounts(self) -> list[dict]:
        data = await self._request("GET", "/openapi/account/list")
        rows = data if isinstance(data, list) else data.get("data") or []
        return [{
            "account_id": str(r.get("account_id") or r.get("accountId") or ""),
            "account_number": r.get("account_number") or r.get("accountNumber"),
            "account_type": r.get("account_type") or r.get("accountType"),
        } for r in rows if isinstance(r, dict)]

    async def get_balance(self, account_id: str,
                          currency: str = "USD") -> dict:
        data = await self._request(
            "GET", "/openapi/assets/balance",
            params={"account_id": account_id, "total_asset_currency": currency})
        r = data.get("data") if isinstance(data, dict) and "data" in data else data
        r = r or {}
        currencies = r.get("account_currency_assets") or []
        primary = next((c for c in currencies
                        if c.get("currency") == currency), currencies[0] if currencies else {})
        return {
            "account_id": account_id,
            "currency": currency,
            "total_market_value": _f(r.get("total_market_value")),
            "total_cash": _f(primary.get("total_cash") or primary.get("cash_balance")),
            "buying_power": _f(primary.get("buying_power") or primary.get("stock_power")),
            "unrealized_profit_loss": _f(r.get("total_unrealized_profit_loss")
                                         or primary.get("unrealized_profit_loss")),
            "margin_utilization_rate": _f(r.get("margin_utilization_rate")),
            "raw_keys": sorted(r.keys()) if isinstance(r, dict) else [],
        }

    async def get_positions(self, account_id: str) -> list[dict]:
        data = await self._request("GET", "/openapi/assets/positions",
                                   params={"account_id": account_id})
        rows = data if isinstance(data, list) else (
            data.get("data") or data.get("holdings") or [])
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            items = r.get("items") or [r]
            first = items[0] if items else {}
            out.append({
                "position_id": r.get("position_id") or r.get("id"),
                "symbol": first.get("symbol") or r.get("symbol"),
                "instrument_type": (first.get("instrument_type")
                                    or r.get("instrument_type")),
                "quantity": _f(r.get("quantity") or r.get("qty")),
                "cost_price": _f(r.get("cost_price") or r.get("unit_cost")),
                "market_value": _f(r.get("market_value")),
                "unrealized_profit_loss": _f(r.get("unrealized_profit_loss")),
                "side": first.get("side"),
            })
        return out

    # ---- market data -----------------------------------------------------

    async def get_quote(self, symbol: str) -> Quote:
        data = await self._request(
            "GET", "/openapi/market-data/stock/snapshot",
            params={"symbols": symbol.upper(), "category": "US_STOCK"})
        rows = data if isinstance(data, list) else data.get("data") or []
        if not rows:
            raise ProviderError(f"Webull returned no snapshot for {symbol}",
                                provider=self.name)
        r = rows[0]
        last = _f(r.get("close") or r.get("price") or r.get("last"))
        prev = _f(r.get("preClose") or r.get("pre_close"))
        return Quote(
            symbol=symbol.upper(), last=last or 0.0,
            bid=_f(r.get("bidPrice") or r.get("bid_price")),
            ask=_f(r.get("askPrice") or r.get("ask_price")),
            open=_f(r.get("open")), high=_f(r.get("high")), low=_f(r.get("low")),
            prev_close=prev, volume=_f(r.get("volume")),
            change=_f(r.get("change")),
            change_pct=_f(r.get("changeRatio")) * 100
            if _f(r.get("changeRatio")) is not None else None,
            timestamp=dt.datetime.now(dt.timezone.utc),
            delayed=False, source=self.name,
        )

    async def get_bars(self, symbol: str, *, timespan: str = "day",
                       limit: int = 250) -> list[Bar]:
        span = {"minute": "M1", "5minute": "M5", "hour": "M60", "day": "D",
                "week": "W", "month": "M"}.get(timespan, "D")
        data = await self._request(
            "GET", "/openapi/market-data/stock/bars",
            params={"symbol": symbol.upper(), "category": "US_STOCK",
                    "timespan": span, "count": min(limit, 1200)})
        rows = data.get("data") or data.get("bars") or []
        out: list[Bar] = []
        for r in rows:
            ts = r.get("tradeTime") or r.get("timeStamp") or r.get("t")
            when = _parse_when(ts)
            out.append(Bar(t=when, o=_f(r.get("open")) or 0.0,
                           h=_f(r.get("high")) or 0.0, l=_f(r.get("low")) or 0.0,
                           c=_f(r.get("close")) or 0.0,
                           v=_f(r.get("volume")) or 0.0))
        out.sort(key=lambda b: b.t)
        return out[-limit:]

    # ---- option chain ----------------------------------------------------

    async def get_option_chain(self, symbol: str, *,
                               expiration: dt.date | None = None,
                               max_expirations: int = 6,
                               strike_window_pct: float = 0.30) -> OptionChain:
        sym = symbol.upper()
        quote = await self.get_quote(sym)
        spot = quote.last

        params = {
            "category": "US_OPTION",
            "underlying_symbols": sym,
            "strike_price_gte": round(spot * (1 - strike_window_pct), 2),
            "strike_price_lte": round(spot * (1 + strike_window_pct), 2),
            "page_size": 200,
        }
        if expiration:
            params["start_date"] = expiration.isoformat()
            params["end_date"] = expiration.isoformat()
        else:
            params["start_date"] = dt.date.today().isoformat()
            params["end_date"] = (dt.date.today()
                                  + dt.timedelta(days=400)).isoformat()

        # There is no separate expirations or strikes endpoint -- this one
        # instrument search, filtered by date and strike, is the whole chain.
        instruments: list[dict] = []
        cursor = None
        for _ in range(10):
            page_params = dict(params)
            if cursor:
                page_params["last_instrument_id"] = cursor
            data = await self._request("GET", "/openapi/instrument/option/contracts",
                                       params=page_params)
            rows = data.get("data") or data.get("contracts") or []
            if not rows:
                break
            instruments.extend(rows)
            cursor = rows[-1].get("instrument_id") or rows[-1].get("instrumentId")
            if not cursor or len(rows) < params["page_size"]:
                break

        if not instruments:
            raise ProviderError(f"Webull returned no option contracts for {sym}",
                                provider=self.name)

        parsed: list[OptionContract] = []
        seen_exp: set[dt.date] = set()
        for r in instruments:
            exp_raw = (r.get("option_expire_date") or r.get("expireDate")
                       or r.get("expiration_date"))
            strike = _f(r.get("strike_price") or r.get("strikePrice"))
            right_raw = (r.get("option_type") or r.get("optionType") or "").upper()
            if not exp_raw or strike is None or right_raw not in ("CALL", "PUT"):
                continue
            try:
                exp = dt.date.fromisoformat(str(exp_raw)[:10])
            except ValueError:
                continue

            if expiration is None:
                if exp not in seen_exp:
                    if len(seen_exp) >= max_expirations:
                        continue
                    seen_exp.add(exp)

            right = "call" if right_raw == "CALL" else "put"
            parsed.append(OptionContract(
                symbol=(r.get("symbol") or build_occ(sym, exp, right, strike)),
                underlying=sym, expiration=exp, strike=strike, right=right,
                exercise_style="american", source=self.name,
            ))

        await self._attach_quotes(parsed, spot)
        return OptionChain(underlying=sym, spot=spot, contracts=parsed,
                           source=self.name)

    async def _attach_quotes(self, contracts: list[OptionContract],
                             spot: float) -> None:
        """
        Batch the snapshot calls (20 symbols max per request) and fill in
        pricing. Where Webull does not return greeks or IV -- which is the
        documented behaviour -- we solve implied vol from the mid and compute
        greeks locally, so the chain grid is never blank.
        """
        for i in range(0, len(contracts), OPTION_SNAPSHOT_BATCH):
            batch = contracts[i:i + OPTION_SNAPSHOT_BATCH]
            symbols = ",".join(c.symbol for c in batch)
            try:
                data = await self._request(
                    "GET", "/openapi/market-data/option/snapshot",
                    params={"symbols": symbols, "category": "US_OPTION"})
            except ProviderError as exc:
                log.warning("Webull option snapshot batch failed: %s", exc)
                continue

            rows = data if isinstance(data, list) else (data.get("data") or [])
            by_symbol = {r.get("symbol"): r for r in rows if r.get("symbol")}

            for c in batch:
                r = by_symbol.get(c.symbol)
                if not r:
                    continue
                c.bid = _f(r.get("bidPrice") or r.get("bid_price"))
                c.ask = _f(r.get("askPrice") or r.get("ask_price"))
                c.last = _f(r.get("close") or r.get("price") or r.get("last"))
                c.volume = _f(r.get("volume"))
                c.open_interest = _f(r.get("openInterest") or r.get("open_interest"))
                c.implied_vol = _f(r.get("impliedVolatility") or r.get("implied_volatility"))
                c.delta = _f(r.get("delta"))
                c.gamma = _f(r.get("gamma"))
                c.theta = _f(r.get("theta"))
                c.vega = _f(r.get("vega"))
                c.rho = _f(r.get("rho"))

            self._fill_missing_analytics(batch, spot)

            # Option snapshot is capped at 60 requests/minute.
            await asyncio.sleep(1.05)

    def _fill_missing_analytics(self, contracts: list[OptionContract],
                                spot: float) -> None:
        for c in contracts:
            mid = c.mid
            if not mid or mid <= 0:
                continue
            T = years_from_dte(c.dte)
            if T <= 0:
                continue
            if c.implied_vol is None:
                c.implied_vol = implied_vol(mid, spot, c.strike, T, self.rate,
                                            0.0, c.right, american=True)
            if c.implied_vol and c.delta is None:
                g = bs_greeks(spot, c.strike, T, self.rate, 0.0,
                              c.implied_vol, c.right)
                c.delta, c.gamma = g.delta, g.gamma
                c.theta, c.vega, c.rho = g.theta, g.vega, g.rho


def _f(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_when(value) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    try:
        n = float(value)
        if n > 1e11:            # milliseconds
            n /= 1000.0
        return dt.datetime.fromtimestamp(n, dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)
