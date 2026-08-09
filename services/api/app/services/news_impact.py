"""
Ranking watchlist news by impact.

The obvious way to build this is to ask a model how important each headline
sounds and sort by that. It demos well and it is worthless: it ranks *prose*,
not consequence, so a breathless headline about nothing outranks a terse one
about a guidance cut, and there is no way for the reader to check the ordering
because nothing measurable went into it.

So impact here is measured, from four things that are all observable:

1. **Realized move.** How far the underlying actually moved in the session
   following publication, expressed in units of that name's own trailing daily
   volatility. A 3% day is enormous for a utility and a Tuesday for a small-cap
   biotech; the z-score is what makes those comparable, and it is the single
   heaviest input.

2. **Volume confirmation.** Volume on the reaction day against its own 20-day
   median. A large move on no volume is noise; a large move on triple volume is
   a repricing.

3. **Coverage.** How many separate items landed on the same ticker within a day.
   Clustered coverage is the cheapest available proxy for "this was a real
   event", and it is a fact about the feed rather than a judgement about it.

4. **Event proximity.** Whether the item sits next to a scheduled earnings date,
   which changes how a move should be read.

The thing this deliberately does NOT claim
------------------------------------------
It does not claim the article caused the move. Every scored item carries the
window it measured and the caveat, because the honest statement is "the stock
moved 2.4 sigma in the session after this was published", not "this headline
moved the stock". Most days, most items on a moving stock will score similarly
high — that is the correct behaviour of a co-movement measure and the reason
the response reports the components separately rather than only a total.

Where the bars needed for the window do not exist — an item published in the
last few hours, a symbol with no history — the score is None with a stated
reason. It is never filled in with a default, because a default would sort.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass

from ..providers.base import Bar, NewsItem

TRADING_DAYS = 252.0

# Weights on the components. Deliberately blunt round numbers: the realized
# move is doing most of the work and a finer weighting would imply a precision
# the inputs do not support.
W_MOVE = 0.55
W_VOLUME = 0.20
W_COVERAGE = 0.15
W_EVENT = 0.10


@dataclass
class SymbolContext:
    """Everything needed to score items on one symbol, fetched once."""
    symbol: str
    bars: list[Bar]
    daily_vol: float | None
    median_volume: float | None
    next_earnings: dt.date | None = None

    @classmethod
    def build(cls, symbol: str, bars: list[Bar],
              next_earnings: dt.date | None = None) -> "SymbolContext":
        closes = [b.c for b in bars]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        tail = rets[-60:]
        vol = (math.sqrt(sum(r * r for r in tail) / len(tail))
               if len(tail) >= 20 else None)
        vols = [b.v for b in bars[-20:] if b.v]
        return cls(symbol=symbol.upper(), bars=list(bars), daily_vol=vol,
                   median_volume=(statistics.median(vols) if len(vols) >= 5 else None),
                   next_earnings=next_earnings)


def _reaction_bar(ctx: SymbolContext, published: dt.datetime) -> tuple[Bar, Bar] | None:
    """
    The (prior, reaction) bar pair for an item.

    An item published before the close is measured against that same session;
    one published after the close is measured against the next. Getting this
    backwards would credit a headline with a move that had already happened
    before it was written, which is the classic way this kind of score becomes
    a lagging indicator dressed as a leading one.
    """
    if len(ctx.bars) < 2:
        return None
    pub = published.astimezone(dt.timezone.utc)
    cutoff = pub.date() if pub.hour < 20 else pub.date() + dt.timedelta(days=1)

    for i in range(1, len(ctx.bars)):
        bar_day = ctx.bars[i].t.date()
        if bar_day >= cutoff:
            return ctx.bars[i - 1], ctx.bars[i]
    return None


def score_item(item: NewsItem, ctx: SymbolContext, *,
               coverage: int = 1, today: dt.date | None = None) -> dict:
    """Score one item. Returns the total plus every component that fed it."""
    today = today or dt.date.today()
    pair = _reaction_bar(ctx, item.published)

    if pair is None or ctx.daily_vol in (None, 0):
        return {
            "impact": None,
            "band": "unscored",
            "reason": ("No completed session after publication yet, or not enough "
                       "price history to normalize the move. Scored items sort "
                       "above unscored ones; this is not a low score."),
            "components": {},
        }

    prior, reaction = pair
    move = (reaction.c / prior.c - 1.0) if prior.c else 0.0
    z = abs(move) / ctx.daily_vol if ctx.daily_vol else 0.0

    # Squashed, not linear. The difference between a 1-sigma and a 3-sigma day
    # matters enormously; the difference between 6 and 8 sigma does not change
    # what anyone does about it.
    move_score = min(1.0, math.tanh(z / 2.5))

    if ctx.median_volume and reaction.v:
        vol_ratio = reaction.v / ctx.median_volume
        volume_score = min(1.0, math.tanh(max(0.0, vol_ratio - 1.0)))
    else:
        vol_ratio, volume_score = None, 0.0

    coverage_score = min(1.0, math.log1p(max(0, coverage - 1)) / math.log(6))

    days_to_earnings = None
    event_score = 0.0
    if ctx.next_earnings:
        days_to_earnings = (ctx.next_earnings - today).days
        if -2 <= days_to_earnings <= 10:
            event_score = 1.0 - min(1.0, abs(days_to_earnings) / 10.0)

    total = (W_MOVE * move_score + W_VOLUME * volume_score
             + W_COVERAGE * coverage_score + W_EVENT * event_score)
    impact = round(total * 100, 1)

    return {
        "impact": impact,
        "band": band_for(impact),
        "direction": "up" if move > 0 else "down" if move < 0 else "flat",
        "components": {
            "realized_move_pct": round(move * 100, 3),
            "move_sigma": round(z, 2),
            "move_score": round(move_score, 3),
            "volume_vs_median": round(vol_ratio, 2) if vol_ratio else None,
            "volume_score": round(volume_score, 3),
            "coverage_items_24h": coverage,
            "coverage_score": round(coverage_score, 3),
            "days_to_earnings": days_to_earnings,
            "event_score": round(event_score, 3),
        },
        "window": {
            "published": item.published.isoformat(),
            "prior_close": prior.t.date().isoformat(),
            "reaction_close": reaction.t.date().isoformat(),
        },
        "caveat": ("Measured co-movement, not causation. This says the underlying "
                   "moved this far in the session after the item was published; "
                   "it does not say the item is why."),
    }


def band_for(impact: float) -> str:
    """
    Four bands. Thresholds are in score units and are a display choice, so they
    live in one place and are reported with the score rather than hard-coded
    into a component that renders a colour.
    """
    if impact >= 60:
        return "high"
    if impact >= 35:
        return "elevated"
    if impact >= 15:
        return "moderate"
    return "low"


def rank(items: list[NewsItem], contexts: dict[str, SymbolContext], *,
         today: dt.date | None = None) -> dict:
    """Score and sort a feed. Unscored items sort last, never as zero."""
    coverage: dict[str, int] = {}
    for it in items:
        for t in it.tickers or []:
            coverage[t.upper()] = coverage.get(t.upper(), 0) + 1

    scored = []
    for it in items:
        sym = (it.tickers or [""])[0].upper()
        ctx = contexts.get(sym)
        payload = it.to_dict()
        if ctx is None:
            payload["scoring"] = {
                "impact": None, "band": "unscored", "components": {},
                "reason": f"no price history loaded for {sym or 'this item'}",
            }
        else:
            payload["scoring"] = score_item(it, ctx,
                                            coverage=coverage.get(sym, 1),
                                            today=today)
        payload["symbol"] = sym
        scored.append(payload)

    scored.sort(key=lambda r: (r["scoring"]["impact"] is not None,
                               r["scoring"].get("impact") or 0.0),
                reverse=True)

    counts: dict[str, int] = {}
    for r in scored:
        b = r["scoring"]["band"]
        counts[b] = counts.get(b, 0) + 1

    return {
        "count": len(scored),
        "bands": counts,
        "items": scored,
        "method": (
            "Impact is measured, not inferred from the headline text. It blends "
            "the size of the underlying's move in the session after publication "
            "(in units of that name's own trailing volatility), volume against "
            "its 20-day median, how many items landed on the ticker that day, "
            "and proximity to scheduled earnings. Components are reported "
            "individually so the ranking can be checked rather than trusted."
        ),
        "limits": (
            "This is co-movement. On a day a stock moves hard, most items about "
            "that stock score high whether or not any of them caused it. Items "
            "with no completed session yet are unscored and sort last — that is "
            "an absence of evidence, not a low reading."
        ),
    }
