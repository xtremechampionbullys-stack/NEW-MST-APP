"""Market data endpoints: quotes, bars, option chains, volatility metrics."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query

from ..engine.pricing import bs_greeks, implied_vol, years_from_dte
from ..engine.vol import realized_vol
from ..services.vol_service import atm_iv_from_chain, iv_rank_for
from ..providers.base import ProviderError
from ..providers.registry import get_registry

router = APIRouter(prefix="/api/market", tags=["market"])


def _provider_http_error(exc: ProviderError) -> HTTPException:
    """Differentiate missing live configuration from a vendor-side failure."""
    return HTTPException(503 if exc.provider == "configuration" else 502, str(exc))


@router.get("/providers")
async def providers():
    registry = get_registry()
    return {
        "preferred": registry.preferred(),
        "chain": registry.status(),
        "earnings_chain": registry.earnings_status(),
        **registry.readiness(),
    }


@router.get("/quote/{symbol}")
async def quote(symbol: str, provider: str | None = None):
    try:
        q = await get_registry().quote(symbol, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc
    return q.to_dict()


@router.get("/bars/{symbol}")
async def bars(symbol: str, timespan: str = "day",
               limit: int = Query(250, ge=10, le=5000),
               provider: str | None = None):
    try:
        rows = await get_registry().bars(symbol, timespan=timespan, limit=limit,
                                         provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc
    return {"symbol": symbol.upper(), "timespan": timespan,
            "bars": [b.to_dict() for b in rows]}


@router.get("/chain/{symbol}")
async def chain(symbol: str, expiration: str | None = None,
                max_expirations: int = Query(6, ge=1, le=20),
                strike_window_pct: float = Query(0.30, gt=0.01, le=1.0),
                provider: str | None = None):
    exp = None
    if expiration:
        try:
            exp = dt.date.fromisoformat(expiration)
        except ValueError as exc:
            raise HTTPException(422, "expiration must be YYYY-MM-DD") from exc

    try:
        c = await get_registry().chain(
            symbol, expiration=exp, max_expirations=max_expirations,
            strike_window_pct=strike_window_pct, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    payload = c.to_dict(expiration=exp)
    payload["synthetic"] = c.source == "synthetic"
    return payload


@router.get("/volatility/{symbol}")
async def volatility(symbol: str, provider: str | None = None):
    """
    The volatility dashboard: ATM implied by expiry (term structure), the skew
    across strikes, realized volatility for comparison, and IV rank/percentile.

    Implied vs realized is the single most useful comparison a premium seller
    can make, so it is computed here rather than left to the client.
    """
    registry = get_registry()
    try:
        chain = await registry.chain(symbol, max_expirations=8, provider=provider)
        bars = await registry.bars(symbol, limit=260, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    spot = chain.spot
    closes = [b.c for b in bars]

    term: list[dict] = []
    for exp in chain.expirations():
        contracts = chain.for_expiration(exp)
        if not contracts:
            continue
        atm = min(contracts, key=lambda c: abs(c.strike - spot))
        ivs = [c.implied_vol for c in contracts
               if c.implied_vol and abs(c.strike - spot) / spot < 0.03]
        atm_iv = sum(ivs) / len(ivs) if ivs else atm.implied_vol
        if not atm_iv:
            continue
        dte = atm.dte
        term.append({
            "expiration": exp.isoformat(), "dte": dte, "atm_iv": round(atm_iv, 5),
            "expected_move": round(spot * atm_iv * (dte / 365.0) ** 0.5, 2),
            "expected_move_pct": round(atm_iv * (dte / 365.0) ** 0.5, 5),
        })

    near = chain.nearest_expiration(30)
    skew: list[dict] = []
    if near:
        for c in chain.for_expiration(near):
            if not c.implied_vol:
                continue
            skew.append({
                "strike": c.strike, "right": c.right,
                "iv": round(c.implied_vol, 5),
                "delta": round(c.delta, 4) if c.delta is not None else None,
                "moneyness": round(c.strike / spot - 1, 5),
                "open_interest": c.open_interest,
                "volume": c.volume,
            })

    all_ivs = [c.implied_vol for c in chain.contracts
               if c.implied_vol and 20 <= c.dte <= 60]
    # Interpolated to a constant 30-day maturity so the series does not jump
    # every time an expiration rolls off.
    atm_now = atm_iv_from_chain(chain, 30)
    if atm_now is None:
        atm_now = next((row["atm_iv"] for row in term if row["dte"] >= 20),
                       term[0]["atm_iv"] if term else None)

    rv20 = realized_vol(closes, 20)
    rv60 = realized_vol(closes, 60)
    rank = await iv_rank_for(symbol, atm_now, all_ivs) if atm_now else None

    return {
        "symbol": symbol.upper(),
        "spot": spot,
        "source": chain.source,
        "synthetic": chain.source == "synthetic",
        "atm_iv_30d": atm_now,
        "realized_vol_20d": rv20,
        "realized_vol_60d": rv60,
        # Positive means options are pricing more movement than the stock has
        # recently delivered -- the premium seller's edge, when it persists.
        "variance_risk_premium": (round(atm_now - rv20, 5)
                                  if atm_now and rv20 else None),
        "iv_rank": rank,
        "term_structure": term,
        "skew": skew,
        "skew_expiration": near.isoformat() if near else None,
    }


@router.get("/contract/{option_symbol}")
async def contract_detail(option_symbol: str, underlying: str,
                          provider: str | None = None):
    """Single-contract detail with locally recomputed greeks for cross-checking."""
    registry = get_registry()
    try:
        chain = await registry.chain(underlying, max_expirations=20,
                                     provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    match = next((c for c in chain.contracts
                  if c.symbol.upper().lstrip("O:") == option_symbol.upper().lstrip("O:")),
                 None)
    if not match:
        raise HTTPException(404, f"{option_symbol} not found in the chain")

    payload = match.to_dict()
    mid = match.mid
    if mid and match.dte > 0:
        T = years_from_dte(match.dte)
        solved = implied_vol(mid, chain.spot, match.strike, T, 0.045, 0.0,
                             match.right, american=True)
        payload["solved_iv"] = solved
        if solved:
            g = bs_greeks(chain.spot, match.strike, T, 0.045, 0.0, solved,
                          match.right)
            payload["computed_greeks"] = g.as_dict()
    return payload


@router.get("/keystats/{symbol}")
async def keystats(symbol: str, provider: str | None = None):
    """
    The figures that belong beside a live chart.

    Assembled from four calls that the terminal would otherwise make separately
    — quote, bars, profile, earnings — because four round trips to paint one
    sidebar is four chances for it to render half-populated.

    Profile and earnings are allowed to fail without failing the request. A
    quote and a price series are enough to draw the panel; a missing sector or
    an instrument with no earnings is a fact about that instrument, not an
    error, and blanking the whole panel over it would be a worse answer than
    showing what is known.
    """
    from ..services.terminal import key_stats                    # noqa: PLC0415
    registry = get_registry()
    try:
        quote_data = await registry.quote(symbol, provider=provider)
        bar_rows = await registry.bars(symbol, timespan="day", limit=260, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    profile_data = None
    try:
        profile_data = (await registry.profile(symbol, provider=provider)).to_dict()
    except Exception:                                            # noqa: BLE001
        pass

    # Goes through the earnings *service*, not the provider registry. The
    # registry hands back raw events; the service is what resolves which one is
    # next and whether the date is confirmed or projected — and that resolution
    # is precisely what the panel is displaying, so duplicating it here would be
    # a second implementation to drift from the first.
    earnings_data = None
    try:
        from ..providers.earnings_sources import reports_earnings   # noqa: PLC0415
        from ..services.earnings_service import load_events, next_event  # noqa: PLC0415
        if not reports_earnings(symbol):
            earnings_data = {"reports_earnings": False}
        else:
            events, _ = await load_events(symbol)
            nxt = next_event(events)
            earnings_data = {"next_event": nxt.to_dict() if nxt else None}
    except Exception:                                            # noqa: BLE001
        pass

    return key_stats(symbol, quote_data, bar_rows, profile_data, earnings_data)


@router.get("/scan/{symbol}")
async def chart_scan(symbol: str, provider: str | None = None,
                     limit: int = Query(400, ge=60, le=2000)):
    """
    A structural read of the price series: trend, momentum, vol regime, levels.

    Named a scan of the *series*, not of the chart. The distinction is the
    whole point — this is arithmetic on bars, and there is no vision model in
    this application to read a rendered image with.
    """
    from ..services.terminal import scan                          # noqa: PLC0415
    registry = get_registry()
    try:
        quote_data = await registry.quote(symbol, provider=provider)
        bar_rows = await registry.bars(symbol, timespan="day", limit=limit, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc
    return scan(symbol, bar_rows, quote_data)


@router.get("/liquidity/{symbol}")
async def liquidity(symbol: str, provider: str | None = None,
                    limit: int = Query(260, ge=40, le=2000),
                    max_zones: int = Query(6, ge=1, le=12)):
    """
    Volume + order + liquidity map: order blocks with per-zone counters,
    resting liquidity pools, support / resistance and a volume profile.

    Arithmetic on daily bars — there is no order book behind it. See
    services/liquidity.py for exactly what each counter is.
    """
    from ..services.liquidity import liquidity_map                # noqa: PLC0415
    registry = get_registry()
    try:
        bar_rows = await registry.bars(symbol, timespan="day", limit=limit,
                                       provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc
    # Spot is the last bar's close, not the live quote: zones and levels are
    # drawn on the candles, and "above/below price" has to agree with them.
    return liquidity_map(symbol, bar_rows, max_zones=max_zones)


@router.get("/levels/{symbol}")
async def options_levels(symbol: str, expiration: str | None = None,
                         provider: str | None = None):
    """
    Max pain and gamma exposure for one expiration, from the same chain the
    rest of the app prices from. See services/options_levels.py for the
    assumptions — both figures are heuristics with the caveats in the payload.
    """
    from ..services.options_levels import gex, max_pain          # noqa: PLC0415
    registry = get_registry()
    try:
        chain = await registry.chain(symbol, provider=provider)
    except ProviderError as exc:
        raise _provider_http_error(exc) from exc

    # Contract expirations are dates; the API speaks ISO strings. The mapping
    # is explicit so a string/date comparison can never silently match nothing.
    dates = chain.expirations()
    if not dates:
        raise HTTPException(502, f"no contracts in the {symbol} chain")
    by_iso = {d.isoformat(): d for d in dates}
    if expiration is None:
        # The front expiry: where pin and gamma effects concentrate.
        chosen = dates[0]
    else:
        chosen = by_iso.get(expiration)
        if chosen is None:
            raise HTTPException(404, f"{symbol} has no {expiration} expiration; "
                                     f"available: {list(by_iso)[:8]}")

    return {
        "symbol": symbol.upper(),
        "spot": chain.spot,
        "expiration": chosen.isoformat(),
        "expirations": list(by_iso),
        "synthetic": chain.source in ("", "synthetic"),
        "max_pain": max_pain(chain.contracts, chosen),
        "gex": gex(chain.contracts, chosen, chain.spot),
        "note": ("Computed from open interest and greeks in the chain. On the "
                 "synthetic provider these demonstrate the computation rather "
                 "than read real positioning."),
    }
