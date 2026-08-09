"""
The trading terminal's side panels: key statistics and a chart scan.

Two things here, and the second one needs a word of explanation.

**Key stats** are the figures that belong next to a live chart — day range,
52-week range, average volume, market cap, days to the next earnings report.
Nothing clever; the only judgement is refusing to invent the ones that are not
derivable. Average volume is computed over whatever history actually exists and
reports how many sessions that was, because a "30-day average" taken over nine
bars is a different number wearing the same label.

**The scan** is the honest version of a chart-reading feature. It does not look
at the picture — there is no vision model here, and pretending otherwise would
be the single most misleading thing this file could do. It reads the same
series the chart is drawn from and reports structure that is defined
arithmetically: trend by moving-average stack, momentum by RSI, volatility
regime by realized-vol percentile, and support and resistance by swing pivots
that have actually been touched more than once.

Every finding carries the evidence that produced it. A line that says
"resistance at 236.54" is not useful; "resistance at 236.54, touched 3 times,
most recently 12 sessions ago" can be checked against the chart by eye, which
means the user can catch it being wrong. That is the difference between a
signal and an assertion.
"""

from __future__ import annotations

import datetime as dt
import math

from ..providers.base import Bar, Quote


def _sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def _rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI. None when there is not enough history to seed it."""
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def key_stats(symbol: str, quote: Quote, bars: list[Bar],
              profile: dict | None, earnings: dict | None) -> dict:
    closes = [b.c for b in bars]
    highs = [b.h for b in bars]
    lows = [b.l for b in bars]
    vols = [b.v for b in bars if b.v]

    # 52 weeks of daily bars is ~252 sessions. Say how many were actually used:
    # a "52-week range" over 80 bars is a 4-month range with the wrong label.
    window = min(len(bars), 252)
    hi_52 = max(highs[-window:]) if window else None
    lo_52 = min(lows[-window:]) if window else None

    avg_window = min(len(vols), 30)
    avg_vol = sum(vols[-avg_window:]) / avg_window if avg_window else None

    market_cap = None
    if profile:
        market_cap = profile.get("market_cap")
        if not market_cap and profile.get("shares_outstanding"):
            market_cap = profile["shares_outstanding"] * quote.last

    # The earnings payload distinguishes a confirmed date from a projected one,
    # and that distinction has to survive to the panel: "in 25 days" reads as a
    # scheduled fact, and for a projected date it is an extrapolation from the
    # last few reporting cycles.
    days_to_earnings = None
    earnings_note = None
    if earnings:
        if earnings.get("reports_earnings") is False:
            earnings_note = "this instrument does not report earnings"
        nxt = earnings.get("next_event") or {}
        if nxt.get("days_away") is not None and nxt.get("is_upcoming"):
            days_to_earnings = nxt["days_away"]
            if not nxt.get("is_confirmed", True):
                earnings_note = "projected date, not confirmed by the company"

    return {
        "symbol": symbol.upper(),
        "name": (profile or {}).get("name"),
        "exchange": (profile or {}).get("exchange"),
        "sector": (profile or {}).get("sector"),
        "industry": (profile or {}).get("industry"),
        "last": quote.last,
        "change": quote.change,
        "change_pct": quote.change_pct,
        "bid": quote.bid,
        "ask": quote.ask,
        "day_low": quote.low,
        "day_high": quote.high,
        "range_52w": {"low": lo_52, "high": hi_52,
                      "sessions": window,
                      "complete": window >= 252},
        "volume": quote.volume,
        "avg_volume": avg_vol,
        "avg_volume_sessions": avg_window,
        "market_cap": market_cap,
        "days_to_earnings": days_to_earnings,
        "earnings_note": earnings_note,
        "as_of": (quote.timestamp or dt.datetime.now(dt.timezone.utc)).isoformat(),
        "delayed": quote.delayed,
    }


def _pivots(bars: list[Bar], lookback: int = 3) -> tuple[list[float], list[float]]:
    """
    Swing highs and lows: a bar whose high tops `lookback` bars on both sides.

    Requiring both sides is what makes these pivots rather than local maxima of
    a trailing window — the latter marks every new high as a pivot, including
    the most recent bar, which by construction has nothing to its right yet.
    """
    highs, lows = [], []
    for i in range(lookback, len(bars) - lookback):
        window = bars[i - lookback:i + lookback + 1]
        if bars[i].h == max(b.h for b in window):
            highs.append(bars[i].h)
        if bars[i].l == min(b.l for b in window):
            lows.append(bars[i].l)
    return highs, lows


def _cluster(levels: list[float], tolerance: float) -> list[dict]:
    """Group pivots that sit within `tolerance` of each other into one level."""
    out: list[dict] = []
    for lv in sorted(levels):
        if out and abs(lv - out[-1]["price"]) <= tolerance:
            grp = out[-1]
            grp["touches"] += 1
            grp["price"] = (grp["price"] * (grp["touches"] - 1) + lv) / grp["touches"]
        else:
            out.append({"price": lv, "touches": 1})
    return out


def scan(symbol: str, bars: list[Bar], quote: Quote) -> dict:
    """
    A structural read of the price series.

    Reads the data the chart is drawn from — not the rendered chart. There is no
    image model in this application, and a feature that implied one would be
    lying about where its conclusions come from.
    """
    if len(bars) < 30:
        return {
            "symbol": symbol.upper(),
            "findings": [],
            "note": (f"Only {len(bars)} bars available. A structural read needs at least "
                     f"30 before any of it means anything, so nothing is reported rather "
                     f"than reporting it weakly."),
            "reads": "price series",
        }

    closes = [b.c for b in bars]
    spot = quote.last
    findings: list[dict] = []

    # --- trend: moving-average stack -------------------------------------
    ma20, ma50, ma200 = _sma(closes, 20), _sma(closes, 50), _sma(closes, 200)
    if ma20 and ma50:
        if ma200:
            if ma20 > ma50 > ma200:
                state, detail = "uptrend", "20 > 50 > 200-day averages, stacked in order"
            elif ma20 < ma50 < ma200:
                state, detail = "downtrend", "20 < 50 < 200-day averages, stacked in order"
            else:
                state, detail = "no clear trend", "the 20, 50 and 200-day averages are not stacked"
        else:
            state = "uptrend" if ma20 > ma50 else "downtrend"
            detail = f"20-day {'above' if ma20 > ma50 else 'below'} the 50-day; no 200-day yet"
        findings.append({
            "kind": "trend", "label": state, "detail": detail,
            "values": {"ma20": round(ma20, 2), "ma50": round(ma50, 2),
                       "ma200": round(ma200, 2) if ma200 else None},
        })

    # --- momentum ---------------------------------------------------------
    rsi = _rsi(closes)
    if rsi is not None:
        if rsi >= 70:
            lbl, det = "overbought", "RSI at or above 70 — stretched, which is not the same as a sell"
        elif rsi <= 30:
            lbl, det = "oversold", "RSI at or below 30 — stretched, which is not the same as a buy"
        else:
            lbl, det = "neutral momentum", "RSI between 30 and 70"
        findings.append({"kind": "momentum", "label": lbl, "detail": det,
                         "values": {"rsi14": round(rsi, 1)}})

    # --- volatility regime ------------------------------------------------
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    if len(rets) > 60:
        def rv(window: list[float]) -> float:
            m = sum(window) / len(window)
            var = sum((r - m) ** 2 for r in window) / max(len(window) - 1, 1)
            return math.sqrt(var * 252)
        current = rv(rets[-20:])
        history = sorted(rv(rets[i:i + 20]) for i in range(0, len(rets) - 20, 5))
        below = sum(1 for h in history if h < current)
        pct = 100.0 * below / len(history) if history else None
        if pct is not None:
            if pct >= 80:
                lbl = "high volatility"
            elif pct <= 20:
                lbl = "low volatility"
            else:
                lbl = "average volatility"
            findings.append({
                "kind": "volatility", "label": lbl,
                "detail": (f"20-day realized volatility is {current:.0%}, in the "
                           f"{pct:.0f}th percentile of its own last "
                           f"{len(history)} readings"),
                "values": {"realized_20d": round(current, 4), "percentile": round(pct, 1)},
            })

    # --- support and resistance ------------------------------------------
    highs, lows = _pivots(bars[-180:] if len(bars) > 180 else bars)
    tol = spot * 0.015
    res = [c for c in _cluster(highs, tol) if c["price"] > spot]
    sup = [c for c in _cluster(lows, tol) if c["price"] < spot]
    # Nearest first, and only levels touched more than once: a single pivot is
    # one bar's high, which is a fact about that bar and not a level.
    res = sorted([c for c in res if c["touches"] > 1], key=lambda c: c["price"])[:2]
    sup = sorted([c for c in sup if c["touches"] > 1], key=lambda c: -c["price"])[:2]
    for c in res:
        findings.append({
            "kind": "resistance", "label": f"resistance {c['price']:.2f}",
            "detail": (f"{c['touches']} swing highs cluster here, "
                       f"{100 * (c['price'] / spot - 1):.1f}% above spot"),
            "values": {"price": round(c["price"], 2), "touches": c["touches"]},
        })
    for c in sup:
        findings.append({
            "kind": "support", "label": f"support {c['price']:.2f}",
            "detail": (f"{c['touches']} swing lows cluster here, "
                       f"{100 * (1 - c['price'] / spot):.1f}% below spot"),
            "values": {"price": round(c["price"], 2), "touches": c["touches"]},
        })

    return {
        "symbol": symbol.upper(),
        "spot": spot,
        "bars_read": len(bars),
        "findings": findings,
        "reads": "price series",
        "note": (
            "Computed from the price series the chart is drawn from — this does not "
            "look at the rendered image, and there is no vision model in this "
            "application. Every finding above is arithmetic on the bars, and each "
            "carries the numbers that produced it so it can be checked against the "
            "chart by eye. None of it is a forecast."
        ),
    }
