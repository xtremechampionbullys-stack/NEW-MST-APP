"""
Volume + order + liquidity map: order blocks with per-zone counters, resting
liquidity pools, support / resistance, and a volume profile.

Everything here is arithmetic on OHLCV bars. That sentence is the limit of the
whole module and it is worth being exact about what follows from it, because
"order block" and "liquidity" are words that sound like they describe an order
book, and this application has no order book (see DepthPanel in the web app).

What each output IS, mechanically:

**Order block** — the last opposing candle before a move that closed through
the prior confirmed swing (a break of structure). The candle's range is the
zone. It is a price-action construct: a place where one side was absorbed
before price left quickly. It is not a record of resting institutional orders,
which no public feed publishes.

**Zone counters** — for every bar that traded inside a zone after it formed:

* `touches`       separate returns into the zone after the break confirmed it
* `bars_in_zone`  bars whose range overlapped the zone
* `volume`        bar volume, prorated by how much of the bar's range lay
                  inside the zone (the standard volume-profile approximation —
                  a daily bar does not say where inside its range it traded)
* `buy_volume` / `sell_volume`   that volume split by where the bar closed in
                  its range. An ESTIMATE of aggressor side; true buy/sell
                  volume needs the trade tape.
* `orders`        the vendor's per-bar trade count, prorated the same way.
                  These are executed prints, not resting orders, and the field
                  is null when the provider does not report a count.

**Liquidity pool** — two or more swing highs (or lows) at nearly the same
price that have not since been traded through. Stops tend to collect just
beyond such levels; that is an inference from structure, not an observation.

**Support / resistance** — swing pivots clustered by price. A level is
`major` when it has been defended at least three times and scores within half
of the strongest level on the chart — capped at the two strongest per side, so
the label stays rare enough to mean something.

On the synthetic provider the bars are generated, so this demonstrates the
computation rather than reading a market — the payload says which.
"""

from __future__ import annotations

from ..providers.base import Bar

PIVOT_LOOKBACK = 3
ATR_WINDOW = 14
MIN_BARS = 40
PROFILE_BINS = 24
VALUE_AREA = 0.70
MAX_MAJOR_PER_SIDE = 2


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

def _ts(bar: Bar) -> int:
    return int(bar.t.timestamp())


def _atr_series(bars: list[Bar], window: int = ATR_WINDOW) -> list[float]:
    """Rolling mean true range, one value per bar (shorter window at the start)."""
    trs: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.h - b.l)
        else:
            pc = bars[i - 1].c
            trs.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    out: list[float] = []
    running = 0.0
    for i, tr in enumerate(trs):
        running += tr
        if i >= window:
            running -= trs[i - window]
        out.append(running / min(i + 1, window))
    return out


def _pivots(bars: list[Bar], k: int = PIVOT_LOOKBACK) -> tuple[list[int], list[int]]:
    """
    Indices of swing highs and swing lows.

    A pivot needs `k` bars on BOTH sides, so the most recent `k` bars can never
    be one — and a pivot at index i is only knowable at i + k. Everything that
    walks the series forward below respects that, so no zone is ever drawn from
    information that did not exist yet on the bar where it appears.
    """
    highs: list[int] = []
    lows: list[int] = []
    for i in range(k, len(bars) - k):
        window = bars[i - k:i + k + 1]
        if bars[i].h == max(b.h for b in window):
            highs.append(i)
        if bars[i].l == min(b.l for b in window):
            lows.append(i)
    return highs, lows


def _overlap_fraction(bar: Bar, bottom: float, top: float) -> float:
    """Share of the bar's range that lies inside [bottom, top]."""
    span = bar.h - bar.l
    if span <= 0:
        return 1.0 if bottom <= bar.c <= top else 0.0
    return max(0.0, min(bar.h, top) - max(bar.l, bottom)) / span


def _buy_fraction(bar: Bar) -> float:
    """Close location in the bar's range: 1.0 closed on the high, 0.0 on the low."""
    span = bar.h - bar.l
    return 0.5 if span <= 0 else (bar.c - bar.l) / span


# --------------------------------------------------------------------------
# Order blocks
# --------------------------------------------------------------------------

def _find_order_blocks(bars: list[Bar], atr: list[float],
                       highs: list[int], lows: list[int]) -> list[dict]:
    """
    Walk the series forward; each close through the latest confirmed swing is a
    break of structure, and the last opposing candle before the breaking leg is
    the order block.
    """
    k = PIVOT_LOOKBACK
    blocks: list[dict] = []
    hi_ptr = lo_ptr = 0
    swing_hi: int | None = None      # latest confirmed, not yet broken
    swing_lo: int | None = None

    for i, bar in enumerate(bars):
        while hi_ptr < len(highs) and highs[hi_ptr] + k <= i:
            swing_hi = highs[hi_ptr]
            hi_ptr += 1
        while lo_ptr < len(lows) and lows[lo_ptr] + k <= i:
            swing_lo = lows[lo_ptr]
            lo_ptr += 1

        if swing_hi is not None and bar.c > bars[swing_hi].h:
            # The leg that broke the high starts at the lowest low since it.
            origin = min(range(swing_hi, i + 1), key=lambda j: bars[j].l)
            ob = next((j for j in range(origin, max(swing_hi, origin - 10) - 1, -1)
                       if bars[j].c < bars[j].o), None)
            if ob is not None and ob < i:
                blocks.append(_block("bullish", bars, atr, ob, i))
            swing_hi = None

        if swing_lo is not None and bar.c < bars[swing_lo].l:
            origin = max(range(swing_lo, i + 1), key=lambda j: bars[j].h)
            ob = next((j for j in range(origin, max(swing_lo, origin - 10) - 1, -1)
                       if bars[j].c > bars[j].o), None)
            if ob is not None and ob < i:
                blocks.append(_block("bearish", bars, atr, ob, i))
            swing_lo = None

    return blocks


def _block(side: str, bars: list[Bar], atr: list[float], ob: int, confirmed: int) -> dict:
    b = bars[ob]
    bottom, top = b.l, b.h
    # An unusually tall candle makes a zone too wide to mean anything. Past
    # 1.5 ATR, keep the wick-to-open half: the side price left from.
    if top - bottom > 1.5 * atr[ob]:
        if side == "bullish":
            top = b.o
        else:
            bottom = b.o
    return {"side": side, "bottom": bottom, "top": top,
            "ob_index": ob, "confirmed_index": confirmed}


def _count_zone(block: dict, bars: list[Bar], avg_volume: float,
                has_trades: bool) -> dict:
    """Run the counters for one zone from its candle to invalidation or today."""
    bottom, top = block["bottom"], block["top"]
    side = block["side"]
    ob, confirmed = block["ob_index"], block["confirmed_index"]

    volume = buy = sell = orders = 0.0
    bars_in_zone = touches = 0
    inside_prev = True               # the block candle is inside by definition
    broken_index: int | None = None
    last_touch: int | None = None

    for j in range(ob, len(bars)):
        bar = bars[j]
        frac = _overlap_fraction(bar, bottom, top)
        inside = frac > 0
        if inside:
            bars_in_zone += 1
            v = bar.v * frac
            bf = _buy_fraction(bar)
            volume += v
            buy += v * bf
            sell += v * (1 - bf)
            if has_trades and bar.n is not None:
                orders += bar.n * frac
            if j > confirmed and not inside_prev:
                touches += 1
                last_touch = j
        if j > confirmed:
            inside_prev = inside
            # A close through the far side ends the zone. Wicks through do not.
            if (side == "bullish" and bar.c < bottom) or \
               (side == "bearish" and bar.c > top):
                broken_index = j
                break
        elif j == confirmed:
            inside_prev = inside

    form = bars[ob]
    return {
        "side": side,
        "bottom": round(bottom, 4),
        "top": round(top, 4),
        "mid": round((bottom + top) / 2, 4),
        "formed_t": _ts(form),
        "confirmed_t": _ts(bars[confirmed]),
        "status": ("broken" if broken_index is not None
                   else "tested" if touches else "fresh"),
        "broken_t": _ts(bars[broken_index]) if broken_index is not None else None,
        "last_touch_t": _ts(bars[last_touch]) if last_touch is not None else None,
        "touches": touches,
        "bars_in_zone": bars_in_zone,
        "volume": round(volume),
        "buy_volume": round(buy),
        "sell_volume": round(sell),
        "delta": round(buy) - round(sell),
        "orders": round(orders) if has_trades else None,
        "formation_volume": round(form.v),
        "formation_rel_volume": round(form.v / avg_volume, 2) if avg_volume else None,
    }


def _select_zones(zones: list[dict], spot: float, limit: int) -> list[dict]:
    """
    Live zones only, newest first, dropping any that overlap a newer zone on
    the same side — stacked boxes over one price range read as one zone with a
    confusing set of counters.
    """
    kept: list[dict] = []
    for z in sorted((z for z in zones if z["status"] != "broken"),
                    key=lambda z: z["confirmed_t"], reverse=True):
        if any(k["side"] == z["side"] and z["bottom"] <= k["top"] and z["top"] >= k["bottom"]
               for k in kept):
            continue
        kept.append(z)
    kept.sort(key=lambda z: abs(z["mid"] - spot))
    kept = kept[:limit]
    for n, z in enumerate(sorted(kept, key=lambda z: -z["mid"]), start=1):
        z["id"] = f"OB{n}"
        z["distance_pct"] = round((z["mid"] / spot - 1) * 100, 2) if spot else None
    return sorted(kept, key=lambda z: -z["mid"])


# --------------------------------------------------------------------------
# Liquidity pools and support / resistance
# --------------------------------------------------------------------------

def _cluster(points: list[tuple[float, int]], tolerance: float) -> list[list[tuple[float, int]]]:
    """
    Group (price, index) pivots into bands no wider than `tolerance`.

    Width is measured from the band's lowest pivot, not its latest one.
    Chaining neighbour to neighbour lets a staircase of pivots a few cents
    apart merge into one "level" spanning half the chart with fifty touches.
    """
    groups: list[list[tuple[float, int]]] = []
    for p in sorted(points):
        if groups and p[0] - groups[-1][0][0] <= tolerance:
            groups[-1].append(p)
        else:
            groups.append([p])
    return groups


def _liquidity_pools(bars: list[Bar], highs: list[int], lows: list[int],
                     atr_now: float, spot: float, limit: int = 4) -> list[dict]:
    pools: list[dict] = []
    tol = 0.25 * atr_now

    for side, idxs in (("buy", highs), ("sell", lows)):
        pts = [((bars[i].h if side == "buy" else bars[i].l), i) for i in idxs]
        for grp in _cluster(pts, tol):
            if len(grp) < 2:
                continue
            last = max(i for _, i in grp)
            first = min(i for _, i in grp)
            # Buy-side liquidity rests ABOVE equal highs, sell-side BELOW lows.
            price = max(p for p, _ in grp) if side == "buy" else min(p for p, _ in grp)
            after = bars[last + 1:]
            swept = (any(b.h > price for b in after) if side == "buy"
                     else any(b.l < price for b in after))
            if swept:
                continue
            pools.append({
                "side": side,
                "price": round(price, 4),
                "touches": len(grp),
                "first_t": _ts(bars[first]),
                "last_t": _ts(bars[last]),
                "distance_pct": round((price / spot - 1) * 100, 2) if spot else None,
            })

    out: list[dict] = []
    for side in ("buy", "sell"):
        mine = sorted((p for p in pools if p["side"] == side),
                      key=lambda p: abs(p["price"] - spot))[:limit]
        out.extend(mine)
    return sorted(out, key=lambda p: -p["price"])


def _levels(bars: list[Bar], highs: list[int], lows: list[int], atr_now: float,
            avg_volume: float, spot: float, per_side: int = 4) -> list[dict]:
    n = len(bars)
    pts = [(bars[i].h, i) for i in highs] + [(bars[i].l, i) for i in lows]
    levels: list[dict] = []
    for grp in _cluster(pts, 0.6 * atr_now):
        if len(grp) < 2:
            continue
        price = sum(p for p, _ in grp) / len(grp)
        # Each defence counts for more if it was recent and on heavy volume.
        score = 0.0
        for _, i in grp:
            recency = 0.5 + 0.5 * (i / max(n - 1, 1))
            weight = min(2.0, bars[i].v / avg_volume) if avg_volume else 1.0
            score += recency * (0.5 + 0.5 * weight)
        levels.append({
            "price": round(price, 4),
            "touches": len(grp),
            "score": score,
            "last_t": _ts(bars[max(i for _, i in grp)]),
            "volume_at_touches": round(sum(bars[i].v for _, i in grp)),
        })
    if not levels:
        return []

    top = max(lv["score"] for lv in levels)
    for lv in levels:
        lv["kind"] = "support" if lv["price"] < spot else "resistance"
        lv["strength"] = round(100 * lv["score"] / top)
        lv["major"] = False
        lv["distance_pct"] = round((lv["price"] / spot - 1) * 100, 2) if spot else None
    # "Major" has to stay rare to mean anything: at most the two strongest
    # qualifying levels on each side of price.
    for kind in ("resistance", "support"):
        eligible = sorted((lv for lv in levels if lv["kind"] == kind
                           and lv["touches"] >= 3 and lv["score"] >= 0.5 * top),
                          key=lambda lv: -lv["score"])
        for lv in eligible[:MAX_MAJOR_PER_SIDE]:
            lv["major"] = True
    for lv in levels:
        del lv["score"]

    out: list[dict] = []
    for kind in ("resistance", "support"):
        mine = [lv for lv in levels if lv["kind"] == kind]
        # Majors are always kept; the remaining slots go to the nearest levels.
        majors = [lv for lv in mine if lv["major"]]
        rest = sorted((lv for lv in mine if not lv["major"]),
                      key=lambda lv: abs(lv["price"] - spot))
        chosen = (sorted(majors, key=lambda lv: abs(lv["price"] - spot))[:per_side]
                  + rest)[:per_side]
        out.extend(chosen)
    return sorted(out, key=lambda lv: -lv["price"])


# --------------------------------------------------------------------------
# Volume profile
# --------------------------------------------------------------------------

def _profile(bars: list[Bar], has_trades: bool, bins: int = PROFILE_BINS) -> dict | None:
    lo = min(b.l for b in bars)
    hi = max(b.h for b in bars)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    rows = [{"lo": lo + i * step, "hi": lo + (i + 1) * step,
             "volume": 0.0, "buy_volume": 0.0, "orders": 0.0} for i in range(bins)]
    for b in bars:
        first = min(bins - 1, max(0, int((b.l - lo) / step)))
        last = min(bins - 1, max(0, int((b.h - lo) / step)))
        bf = _buy_fraction(b)
        for i in range(first, last + 1):
            frac = _overlap_fraction(b, rows[i]["lo"], rows[i]["hi"])
            if b.h - b.l <= 0:
                frac = 1.0 if i == first else 0.0
            rows[i]["volume"] += b.v * frac
            rows[i]["buy_volume"] += b.v * frac * bf
            if has_trades and b.n is not None:
                rows[i]["orders"] += b.n * frac

    total = sum(r["volume"] for r in rows)
    if total <= 0:
        return None
    poc = max(range(bins), key=lambda i: rows[i]["volume"])

    # Value area: grow outward from the point of control, always taking the
    # heavier neighbour, until 70% of the volume is enclosed.
    a = z = poc
    enclosed = rows[poc]["volume"]
    while enclosed < VALUE_AREA * total and (a > 0 or z < bins - 1):
        below = rows[a - 1]["volume"] if a > 0 else -1.0
        above = rows[z + 1]["volume"] if z < bins - 1 else -1.0
        if above >= below:
            z += 1
            enclosed += rows[z]["volume"]
        else:
            a -= 1
            enclosed += rows[a]["volume"]

    return {
        "bins": [{
            "lo": round(r["lo"], 4), "hi": round(r["hi"], 4),
            "volume": round(r["volume"]),
            "buy_volume": round(r["buy_volume"]),
            "sell_volume": round(r["volume"] - r["buy_volume"]),
            "orders": round(r["orders"]) if has_trades else None,
        } for r in rows],
        "poc": round((rows[poc]["lo"] + rows[poc]["hi"]) / 2, 4),
        "value_area_low": round(rows[a]["lo"], 4),
        "value_area_high": round(rows[z]["hi"], 4),
        "total_volume": round(total),
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

METHOD = (
    "Order blocks: last opposing candle before a close through the prior "
    "confirmed 3-bar swing. Counters accumulate over every bar that traded in "
    "the zone since; volume and trade counts are prorated by the share of each "
    "bar's range inside the zone. Liquidity: two or more swing highs/lows "
    "inside a 0.25 ATR band, not yet traded through. Support/resistance: swing pivots "
    "clustered into 0.6 ATR bands; major = three or more touches and at least half "
    "the strongest level's score, at most two per side."
)

LIMITS = (
    "Computed from OHLCV bars only. There is no order book and no trade tape "
    "behind this: 'orders' is the vendor's count of executed trades per bar, "
    "not resting orders, and buy/sell volume is estimated from where each bar "
    "closed in its range. Zones and levels describe where price reacted "
    "before. They are not forecasts."
)


def liquidity_map(symbol: str, bars: list[Bar], spot: float | None = None, *,
                  max_zones: int = 6) -> dict:
    """The full indicator payload for one symbol's bar series."""
    bars = [b for b in bars if b.h >= b.l and b.h > 0]
    base = {
        "symbol": symbol.upper(),
        "bars_read": len(bars),
        "method": METHOD,
        "limits": LIMITS,
    }
    if len(bars) < MIN_BARS:
        return {
            **base,
            "spot": spot, "atr": None, "from_t": None, "to_t": None,
            "synthetic": False, "order_count_source": None,
            "order_blocks": [], "liquidity": [], "levels": [], "profile": None,
            "summary": None,
            "note": (f"Only {len(bars)} bars available. Swings, zones and levels "
                     f"need at least {MIN_BARS} before any of them mean anything, "
                     f"so nothing is reported rather than reporting it weakly."),
        }

    spot = float(spot) if spot else bars[-1].c
    atr = _atr_series(bars)
    atr_now = atr[-1] or (spot * 0.01)
    vols = [b.v for b in bars if b.v]
    avg_volume = sum(vols) / len(vols) if vols else 0.0
    has_trades = any(b.n is not None for b in bars)
    synthetic = all(b.source == "synthetic" for b in bars)

    highs, lows = _pivots(bars)
    raw = _find_order_blocks(bars, atr, highs, lows)
    counted = [_count_zone(b, bars, avg_volume, has_trades) for b in raw]
    zones = _select_zones(counted, spot, max_zones)
    pools = _liquidity_pools(bars, highs, lows, atr_now, spot)
    levels = _levels(bars, highs, lows, atr_now, avg_volume, spot)

    supports = [lv for lv in levels if lv["kind"] == "support"]
    resistances = [lv for lv in levels if lv["kind"] == "resistance"]
    summary = {
        "nearest_support": max((lv["price"] for lv in supports), default=None),
        "nearest_resistance": min((lv["price"] for lv in resistances), default=None),
        "major_support": max((lv["price"] for lv in supports if lv["major"]), default=None),
        "major_resistance": min((lv["price"] for lv in resistances if lv["major"]),
                                default=None),
        "bullish_zones": sum(1 for z in zones if z["side"] == "bullish"),
        "bearish_zones": sum(1 for z in zones if z["side"] == "bearish"),
        "zones_detected": len(counted),
        "zones_broken": sum(1 for z in counted if z["status"] == "broken"),
    }

    return {
        **base,
        "spot": round(spot, 4),
        "atr": round(atr_now, 4),
        "from_t": _ts(bars[0]),
        "to_t": _ts(bars[-1]),
        "synthetic": synthetic,
        "order_count_source": (None if not has_trades
                               else "generated" if synthetic else "provider trade count"),
        "order_blocks": zones,
        "liquidity": pools,
        "levels": levels,
        "profile": _profile(bars, has_trades),
        "summary": summary,
        "note": ("Bars are generated by the synthetic provider, so this "
                 "demonstrates the computation rather than reading a market."
                 if synthetic else
                 "Computed from the provider's bars; see `limits` for what the "
                 "counters can and cannot tell you."),
    }


__all__ = ["liquidity_map", "MIN_BARS"]
