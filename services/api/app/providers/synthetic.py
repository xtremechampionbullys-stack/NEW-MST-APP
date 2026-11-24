"""
Synthetic market data provider.

This is not a toy. It generates a self-consistent market -- a price series with
realistic drift and volatility clustering, an option chain priced off a skewed
volatility surface, bid/ask spreads that widen with moneyness and thin out in
the wings, open interest concentrated at round strikes, and standard monthly
plus weekly expirations on real calendar dates.

Two reasons it exists:

1. The entire product runs with zero API keys and zero cost. Every chart, every
   strategy, every analytic works on day one.
2. It is deterministic per symbol (seeded from the ticker), so screenshots,
   tests, and demos reproduce exactly.

It is clearly labelled as synthetic everywhere it surfaces, because presenting
generated data as market data would be indefensible.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import random

from ..engine.pricing import bs_greeks, price_option, years_from_dte
from ..engine.vol import ParametricVolSurface
from .earnings_sources import synthetic_earnings_dates, synthetic_earnings_profile
from .base import (
    Bar,
    BaseProvider,
    CompanyProfile,
    Fundamentals,
    NewsItem,
    OptionChain,
    OptionContract,
    Quote,
    build_occ,
)

# Anchors so synthetic names look plausible rather than random.
# (anchor price, annual vol, name, sector, beta to the market factor)
_SEEDS: dict[str, tuple[float, float, str, str, float]] = {
    "SPY": (598.0, 0.14, "SPDR S&P 500 ETF Trust", "Index ETF", 1.00),
    "QQQ": (525.0, 0.19, "Invesco QQQ Trust", "Index ETF", 1.12),
    "IWM": (232.0, 0.21, "iShares Russell 2000 ETF", "Index ETF", 1.08),
    "AAPL": (232.0, 0.26, "Apple Inc.", "Technology", 1.15),
    "MSFT": (438.0, 0.24, "Microsoft Corporation", "Technology", 1.05),
    "NVDA": (172.0, 0.48, "NVIDIA Corporation", "Semiconductors", 1.95),
    "TSLA": (331.0, 0.55, "Tesla, Inc.", "Automotive", 1.80),
    "AMZN": (222.0, 0.29, "Amazon.com, Inc.", "Consumer Discretionary", 1.25),
    "META": (712.0, 0.33, "Meta Platforms, Inc.", "Technology", 1.30),
    "GOOGL": (196.0, 0.27, "Alphabet Inc.", "Technology", 1.10),
    "AMD": (148.0, 0.45, "Advanced Micro Devices, Inc.", "Semiconductors", 1.90),
    "COIN": (285.0, 0.68, "Coinbase Global, Inc.", "Financials", 2.40),
    "SOFI": (16.4, 0.58, "SoFi Technologies, Inc.", "Financials", 1.70),
    "F": (11.2, 0.32, "Ford Motor Company", "Automotive", 1.20),
    "PLTR": (79.0, 0.62, "Palantir Technologies Inc.", "Technology", 1.85),
}

# One market factor shared by every symbol.
#
# Without this, each synthetic name was an independent random walk: betas came
# out near zero, correlations near zero, and any cross-asset or index analysis
# correctly reported "no relationship" — which made those features impossible to
# demonstrate on default data even though the code was right. Real equities are
# dominated by a common factor, so the generator now has one.
#
# The factor's volatility follows a GARCH-style recursion rather than being
# constant, which gives the series genuine volatility clustering — the property
# that makes HAR and GARCH forecasts meaningful in the first place.
_MARKET_SEED = 0x4D4B5431
_MARKET_CACHE: dict[int, list[float]] = {}


def market_factor(days: int) -> list[float]:
    """Daily market returns, deterministic and shared across all symbols."""
    if days in _MARKET_CACHE:
        return _MARKET_CACHE[days]
    longest = max([days, *(_MARKET_CACHE.keys() or [0])])
    rng = random.Random(_MARKET_SEED)
    target_var = (0.15 ** 2) / 252.0
    omega, alpha, beta = target_var * 0.06, 0.09, 0.85
    var = target_var
    prev = 0.0
    out: list[float] = []
    for _ in range(longest + 40):
        var = omega + alpha * prev * prev + beta * var
        r = rng.gauss(0.0004, math.sqrt(var))
        prev = r
        out.append(r)
    _MARKET_CACHE[days] = out[-days:] if days <= len(out) else out
    return _MARKET_CACHE[days]


def _seed_for(symbol: str) -> int:
    return int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:8], 16)


def _profile_for(symbol: str) -> tuple[float, float, str, str, float]:
    s = symbol.upper()
    if s in _SEEDS:
        return _SEEDS[s]
    rng = random.Random(_seed_for(s))
    price = round(math.exp(rng.uniform(2.5, 6.2)), 2)
    vol = round(rng.uniform(0.22, 0.65), 3)
    beta = round(rng.uniform(0.7, 2.0), 2)
    return price, vol, f"{s} Holdings, Inc.", "Diversified", beta


def _third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 1)
    fridays = [d.replace(day=x) for x in range(1, 22)
               if d.replace(day=x).weekday() == 4]
    return fridays[2]


def standard_expirations(today: dt.date | None = None, count: int = 10) -> list[dt.date]:
    """
    Weeklies for the next six Fridays, then standard monthlies (third Friday)
    out about a year, then two LEAPS cycles. This mirrors how listed chains are
    actually laid out, which matters because DTE choice is a first-class input
    to every strategy in the tester.
    """
    today = today or dt.date.today()
    out: list[dt.date] = []

    d = today + dt.timedelta(days=1)
    while len(out) < 6:
        if d.weekday() == 4:
            out.append(d)
        d += dt.timedelta(days=1)

    y, m = today.year, today.month
    for _ in range(14):
        m += 1
        if m > 12:
            m = 1
            y += 1
        exp = _third_friday(y, m)
        if exp > today and exp not in out:
            out.append(exp)

    for extra_year in (1, 2):
        exp = _third_friday(today.year + extra_year, 1)
        if exp > today and exp not in out:
            out.append(exp)

    return sorted(out)[:count]


def strike_increment(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 250:
        return 2.5
    if price < 1000:
        return 5.0
    return 10.0


class SyntheticProvider(BaseProvider):
    name = "synthetic"
    supports_greeks = True
    is_realtime = False

    def __init__(self, rate: float = 0.045):
        self.rate = rate

    async def is_available(self) -> bool:
        return True

    # ---- price ----------------------------------------------------------

    # One master series per symbol, generated once and sliced. See the note in
    # _series for why the length must not depend on what the caller asked for.
    _MASTER_DAYS = 3000
    _series_cache: dict[str, list] = {}

    def _series(self, symbol: str, days: int = 500) -> list[Bar]:
        """
        Daily bars, sliced from a fixed-length master series.

        The length used to depend on `days`, which meant the generator drew a
        DIFFERENT random walk for every request size -- the same calendar date
        returned a different close depending on how much history you asked for.
        119 of 120 overlapping dates disagreed between a 120-bar and a 400-bar
        request.

        Nothing raised. What it silently broke is comparability: a 900-day
        backtest and a 1200-day dojo run on the same name were scoring rules
        against two unrelated price histories, so their results could not be
        read against each other -- and the dojo's whole job is to compare.
        """
        key = symbol.upper()
        cached = self._series_cache.get(key)
        if cached is None:
            cached = self._generate(key, self._MASTER_DAYS)
            self._series_cache[key] = cached
        return cached[-days:] if days < len(cached) else cached

    def _generate(self, symbol: str, days: int) -> list[Bar]:
        anchor, vol, _, _, beta = _profile_for(symbol)
        rng = random.Random(_seed_for(symbol))

        # A two-factor return model: r = beta * market + idiosyncratic.
        #
        # Total variance is held at the symbol's target, with the market
        # contributing beta^2 * market_var and the residual making up the
        # difference. So a high-beta name really does co-move with the index,
        # and the split between systematic and idiosyncratic risk is a real
        # property of the data rather than an artefact.
        mkt = market_factor(days + 1)
        market_var = (0.15 ** 2) / 252.0
        total_var = (vol ** 2) / 252.0
        idio_var = max(total_var - (beta ** 2) * market_var, total_var * 0.10)

        # Idiosyncratic volatility clusters too, on its own GARCH recursion.
        omega, a, b = idio_var * 0.06, 0.10, 0.84
        var = idio_var
        prev = 0.0

        rets = []
        day_vols = []
        for i in range(days):
            var = omega + a * prev * prev + b * var
            e = rng.gauss(0.0, math.sqrt(var))
            prev = e
            rets.append(beta * mkt[i] + e + 0.0002)
            # Total daily sigma, used below so the high/low range reflects that
            # day's volatility. Previously the range was fixed-width noise,
            # which made range-based volatility estimators read pure static.
            day_vols.append(math.sqrt((beta ** 2) * market_var + var))

        # Build forward then rescale so today's close is exactly the anchor --
        # deterministic and stable across calls, as before.
        closes = [1.0]
        for r in rets:
            closes.append(closes[-1] * math.exp(r))
        scale = anchor / closes[-1]
        closes = [c * scale for c in closes]

        # Anchor the series to the most recent weekday and lay the bars out
        # BACKWARDS from there.
        #
        # The previous version walked forward from `today - days*1.45` and
        # stopped when it ran out of closes, which left the last bar two to
        # four days short of the present. Nothing failed loudly: the dashboard
        # just quoted a stale close, forecasts reported an "as of" date in the
        # past, and news impact scoring found no completed session after any
        # recent headline and marked every item unscored. A feed whose last bar
        # is not the last close is wrong in a way that only shows up in the
        # features that ask "what happened after this".
        last_day = dt.date.today()
        while last_day.weekday() >= 5:
            last_day -= dt.timedelta(days=1)

        session_days: list[dt.date] = []
        day = last_day
        while len(session_days) < len(closes):
            if day.weekday() < 5:
                session_days.append(day)
            day -= dt.timedelta(days=1)
        session_days.reverse()

        # Trade counts come from their own generator so that adding them did
        # not shift a single draw of `rng` above: every price and volume in the
        # series is byte-for-byte what it was before this field existed.
        trade_rng = random.Random(_seed_for(symbol) ^ 0x7ADE5)
        lot = trade_rng.uniform(110.0, 320.0)       # typical shares per print

        bars: list[Bar] = []
        for i, day in enumerate(session_days):
            c = closes[i]
            sig = day_vols[min(max(i - 1, 0), len(day_vols) - 1)]
            o = c * math.exp(rng.gauss(0, sig * 0.6))
            # Expected daily range for Brownian motion is about 1.6 sigma.
            h = max(o, c) * math.exp(abs(rng.gauss(0, sig * 0.8)))
            l = min(o, c) * math.exp(-abs(rng.gauss(0, sig * 0.8)))
            bars.append(Bar(
                t=dt.datetime.combine(day, dt.time(20, 0), dt.timezone.utc),
                o=round(o, 2), h=round(h, 2), l=round(l, 2), c=round(c, 2),
                v=float(int(rng.uniform(2e6, 6e7)))))
            bars[-1].n = int(bars[-1].v / (lot * trade_rng.uniform(0.7, 1.4)))
            bars[-1].source = self.name
        return bars

    async def get_bars(self, symbol: str, *, timespan: str = "day",
                       limit: int = 250) -> list[Bar]:
        return self._series(symbol, days=max(limit, 60))[-limit:]

    def _intraday_drift(self, symbol: str, close: float, day_vol: float) -> float:
        """
        Wander the displayed last price around the day's deterministic close.

        Scope is deliberately narrow. BARS are untouched, so every backtest,
        forecast, dojo run and test reproduces byte for byte — the determinism
        the rest of the system depends on is preserved exactly. Only the quote,
        which is a display value, moves.

        The walk is seeded by the minute, so it is still reproducible within any
        given minute and two clients polling together agree with each other.
        Disable with SYNTHETIC_LIVE_TICKS=false.
        """
        from ..config import get_settings
        if not get_settings().synthetic_live_ticks:
            return close
        minute = int(dt.datetime.now(dt.timezone.utc).timestamp() // 60)
        rng = random.Random(_seed_for(symbol) ^ minute)
        # Intraday range scaled off the day's own volatility, so a quiet name
        # ticks quietly. sqrt(1/390) is one minute of a trading day.
        step = close * day_vol * math.sqrt(1 / 390.0) * rng.gauss(0, 1) * 8
        return round(max(close * 0.9, min(close * 1.1, close + step)), 2)

    async def get_quote(self, symbol: str) -> Quote:
        bars = self._series(symbol, days=30)
        last, prev = bars[-1], bars[-2]
        _, vol, _, _, _ = _profile_for(symbol)
        live_last = self._intraday_drift(symbol, last.c, vol / math.sqrt(252.0))
        return Quote(
            symbol=symbol.upper(),
            last=live_last,
            bid=round(live_last * 0.9998, 2),
            ask=round(live_last * 1.0002, 2),
            open=last.o,
            high=max(last.h, live_last),
            low=min(last.l, live_last),
            prev_close=prev.c, volume=last.v,
            change=round(live_last - prev.c, 2),
            change_pct=round((live_last / prev.c - 1) * 100, 3),
            timestamp=dt.datetime.now(dt.timezone.utc),
            delayed=False, source="synthetic",
        )

    # ---- chain ----------------------------------------------------------

    async def get_option_chain(self, symbol: str, *,
                               expiration: dt.date | None = None,
                               max_expirations: int = 6,
                               strike_window_pct: float = 0.30) -> OptionChain:
        quote = await self.get_quote(symbol)
        spot = quote.last
        _, base_vol, _, _, _ = _profile_for(symbol)
        surface = ParametricVolSurface(spot=spot, atm_vol=base_vol,
                                       skew=0.18, smile=0.55)
        rng = random.Random(_seed_for(symbol) ^ 0x5EED)

        # Earnings term-structure bump.
        #
        # Real chains invert into an announcement: the same fixed event variance
        # spread over less time makes the front expiry richer. We add it the way
        # the model says it works -- variance is additive, so an expiry spanning
        # the event carries jump_variance/T on top of the diffusive variance.
        # Without this the generated surface has no event in it at all, and the
        # term-structure decomposition would correctly report "no event premium"
        # on every symbol, which would look like a broken feature rather than an
        # honest reading of flat data.
        _, _, jump_vol = synthetic_earnings_profile(symbol)
        next_earnings = next((d for d in synthetic_earnings_dates(symbol)
                              if d >= dt.date.today()), None)   # None for ETFs

        today = dt.date.today()
        exps = [expiration] if expiration else \
            standard_expirations(today, count=max_expirations)

        inc = strike_increment(spot)
        lo = math.floor(spot * (1 - strike_window_pct) / inc) * inc
        hi = math.ceil(spot * (1 + strike_window_pct) / inc) * inc

        contracts: list[OptionContract] = []
        for exp in exps:
            dte = max((exp - today).days, 0)
            if dte == 0:
                continue
            T = years_from_dte(dte)

            k = lo
            while k <= hi + 1e-9:
                moneyness = abs(math.log(k / spot))
                for right in ("call", "put"):
                    iv = surface.iv(k, dte, right)
                    if next_earnings and exp > next_earnings:
                        iv = math.sqrt(iv * iv + (jump_vol * jump_vol) / T)
                    theo = price_option(spot, k, T, self.rate, 0.0, iv, right,
                                        american=(right == "put"))
                    g = bs_greeks(spot, k, T, self.rate, 0.0, iv, right)

                    # Spreads widen with moneyness and shrink with maturity and
                    # liquidity -- the pattern that actually determines whether
                    # a structure is tradable.
                    rel = 0.012 + moneyness * 0.42 + (0.02 if dte < 7 else 0.0)
                    half = max(0.01, theo * rel * 0.5, 0.005)
                    bid = max(round(theo - half, 2), 0.0)
                    ask = round(theo + half, 2)
                    if ask <= bid:
                        ask = round(bid + 0.01, 2)

                    round_bonus = 3.0 if abs(k / inc) % 5 < 1e-9 else 1.0
                    oi = int(max(0, rng.gauss(4000, 1500))
                             * math.exp(-moneyness * 6) * round_bonus)
                    vol_today = int(oi * rng.uniform(0.02, 0.35))

                    contracts.append(OptionContract(
                        symbol=build_occ(symbol, exp, right, k),
                        underlying=symbol.upper(),
                        expiration=exp, strike=round(k, 4), right=right,
                        bid=bid, ask=ask, last=round(theo, 2),
                        volume=vol_today, open_interest=oi,
                        implied_vol=round(iv, 5),
                        delta=round(g.delta, 5), gamma=round(g.gamma, 6),
                        theta=round(g.theta, 5), vega=round(g.vega, 5),
                        rho=round(g.rho, 5),
                        exercise_style="american", source="synthetic",
                    ))
                k += inc

        return OptionChain(underlying=symbol.upper(), spot=spot,
                           contracts=contracts, source="synthetic")

    # ---- reference ------------------------------------------------------

    async def get_profile(self, symbol: str) -> CompanyProfile:
        price, vol, name, sector, _ = _profile_for(symbol)
        rng = random.Random(_seed_for(symbol) ^ 0xC0FFEE)
        shares = rng.uniform(2e8, 1.6e10)
        return CompanyProfile(
            ticker=symbol.upper(), name=name, sector=sector,
            industry=sector, exchange="XNAS",
            description=(f"Synthetic reference profile for {symbol.upper()}. "
                         "Connect Massive or Webull for live company data."),
            market_cap=round(price * shares, 2),
            shares_outstanding=round(shares),
            employees=int(rng.uniform(500, 250000)),
            homepage=f"https://example.com/{symbol.lower()}",
        )

    async def get_fundamentals(self, symbol: str, *, timeframe: str = "quarterly",
                               limit: int = 8) -> list[Fundamentals]:
        rng = random.Random(_seed_for(symbol) ^ 0xF00D)
        rev = rng.uniform(4e8, 9e10)
        growth = rng.uniform(-0.04, 0.11)
        gm = rng.uniform(0.28, 0.72)
        om = gm * rng.uniform(0.25, 0.62)
        nm = om * rng.uniform(0.55, 0.9)

        out: list[Fundamentals] = []
        today = dt.date.today()
        for i in range(limit):
            r = rev / ((1 + growth) ** i)
            assets = r * rng.uniform(1.4, 3.2)
            liabs = assets * rng.uniform(0.35, 0.72)
            equity = assets - liabs
            ni = r * nm
            period = today - dt.timedelta(days=91 * i if timeframe == "quarterly"
                                          else 365 * i)
            out.append(Fundamentals(
                ticker=symbol.upper(), period_end=period.isoformat(),
                timeframe=timeframe,
                revenue=round(r), gross_profit=round(r * gm),
                operating_income=round(r * om), net_income=round(ni),
                ebitda=round(r * om * 1.22),
                eps_basic=round(ni / rng.uniform(3e8, 4e9), 2),
                total_assets=round(assets), total_liabilities=round(liabs),
                equity=round(equity), cash=round(assets * rng.uniform(0.05, 0.28)),
                debt=round(liabs * rng.uniform(0.25, 0.6)),
                free_cash_flow=round(ni * rng.uniform(0.7, 1.5)),
                operating_cash_flow=round(ni * rng.uniform(1.0, 1.9)),
                gross_margin=round(gm, 4), operating_margin=round(om, 4),
                net_margin=round(nm, 4), roe=round(ni / equity, 4),
                current_ratio=round(rng.uniform(0.9, 3.1), 2),
                debt_to_equity=round(liabs / equity, 2),
            ))
        return out

    async def get_news(self, symbols: list[str], *, limit: int = 25) -> list[NewsItem]:
        rng = random.Random(_seed_for("|".join(sorted(symbols))))
        headlines = [
            ("{s} implied volatility climbs ahead of earnings", "article", "neutral"),
            ("Options flow: unusual call activity in {s}", "article", "positive"),
            ("{s} technical setup: key levels into month end", "video", "neutral"),
            ("Analysts revisit {s} price targets", "article", "positive"),
            ("{s} put skew steepens as hedging demand rises", "article", "negative"),
            ("Sector rotation puts {s} back in focus", "video", "positive"),
        ]
        out: list[NewsItem] = []
        now = dt.datetime.now(dt.timezone.utc)
        for i in range(limit):
            sym = symbols[i % len(symbols)] if symbols else "SPY"
            tpl, media, sentiment = headlines[i % len(headlines)]
            out.append(NewsItem(
                id=f"synthetic-{i}",
                title=tpl.format(s=sym.upper()),
                url="https://example.com/news",
                published=now - dt.timedelta(hours=i * rng.uniform(6, 22)),
                publisher="MarketShark Synthetic Feed",
                summary=("Placeholder item. Connect a news provider to populate "
                         "the watchlist feed with live articles and video."),
                tickers=[sym.upper()], sentiment=sentiment, media_type=media,
            ))
        return out
