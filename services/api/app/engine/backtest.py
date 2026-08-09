"""
Options strategy backtester.

Read this before trusting a number it produces
----------------------------------------------
Backtesting options is not backtesting stocks. To mark a multi-leg position on
some Tuesday in 2023 you need what its contracts were *quoted* at on that
Tuesday -- and historical option quotes are expensive, bulky, and usually
absent. The universal temptation is to model the prices instead and present the
output as though it were a P&L record.

This module refuses to blur that line. There are two quote sources behind one
interface, and every result states which one produced it:

* `StoredQuoteSource` -- real historical quotes out of the `option_quotes`
  table. This is a genuine P&L backtest. Available only once the ingest job has
  populated that table.
* `ModelledQuoteSource` -- Black-Scholes prices off a volatility surface
  calibrated to the underlying's own trailing realized volatility plus a
  variance risk premium. This is a *rules* backtest: it tells you how the entry,
  management and exit logic behaves across real price paths. It does not tell
  you what you would have been filled at.

The difference is not academic. A modelled backtest cannot reproduce volatility
crushes, skew shifts, or the bid-ask you actually pay, and those are precisely
where short-premium strategies make and lose their money. Results carry
`quote_source` and a `caveat` string for this reason.

What is modelled honestly regardless of source
----------------------------------------------
* Commissions per contract, both entering and exiting.
* Slippage as a fraction of theoretical value, always against you.
* Assignment at expiry via intrinsic value.
* Position sizing, concurrency limits, and calendar-correct trading days.
* Earnings avoidance, if requested, using the same calendar the live app uses.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Protocol, Sequence

from .payoff import Leg, Position
from .pricing import bs_price, intrinsic, years_from_dte
from .strategies import MarketContext, get_template
from .vol import ParametricVolSurface, realized_vol


# --------------------------------------------------------------------------
# Quote sources
# --------------------------------------------------------------------------

class QuoteSource(Protocol):
    name: str
    is_historical: bool

    def iv(self, on: dt.date, spot: float, strike: float, dte: float,
           right: str) -> float | None: ...


@dataclass
class ModelledQuoteSource:
    """
    Volatility surface reconstructed from the underlying's own history.

    ATM volatility on any given day is the trailing realized volatility scaled
    by a variance risk premium -- implied volatility sits above realized most of
    the time, which is the entire reason premium selling has an edge. Getting
    that scaling roughly right matters more than any other single assumption in
    a modelled backtest, so it is a parameter rather than a constant, and the
    default (1.15) is deliberately conservative.

    Skew and term structure come from the same parametric surface the live app
    uses when a chain has no quotes, so a modelled backtest and a live model
    price agree with each other.
    """

    closes_by_date: dict[dt.date, float]
    ordered_dates: list[dt.date]
    variance_risk_premium: float = 1.15
    vol_window: int = 20
    floor_vol: float = 0.08
    name: str = "modelled"
    is_historical: bool = False

    _cache: dict[dt.date, float] = field(default_factory=dict, repr=False)

    def atm_vol(self, on: dt.date) -> float:
        if on in self._cache:
            return self._cache[on]
        idx = _bisect_date(self.ordered_dates, on)
        window = self.ordered_dates[max(0, idx - self.vol_window - 1):idx + 1]
        closes = [self.closes_by_date[d] for d in window]
        rv = realized_vol(closes, min(self.vol_window, max(len(closes) - 1, 2)))
        vol = max((rv or self.floor_vol) * self.variance_risk_premium, self.floor_vol)
        self._cache[on] = vol
        return vol

    def iv(self, on: dt.date, spot: float, strike: float, dte: float,
           right: str) -> float | None:
        surface = ParametricVolSurface(spot=spot, atm_vol=self.atm_vol(on),
                                       skew=0.18, smile=0.55)
        return surface.iv(strike, dte, right)


@dataclass
class StoredQuoteSource:
    """
    Real historical quotes, keyed (date, expiration, strike, right) -> implied vol.

    Populated from the `option_quotes` table. Falls back to the modelled surface
    for any strike/expiry the archive does not cover -- real chains are sparse in
    the wings and a backtest that skipped every uncovered strike would silently
    select for liquid strikes only, which flatters results.
    """

    quotes: dict[tuple[dt.date, float, float, str], float]
    fallback: ModelledQuoteSource
    name: str = "historical"
    is_historical: bool = True
    misses: int = 0
    hits: int = 0

    def iv(self, on: dt.date, spot: float, strike: float, dte: float,
           right: str) -> float | None:
        key = (on, round(dte, 0), round(strike, 2), right)
        hit = self.quotes.get(key)
        if hit is not None:
            self.hits += 1
            return hit
        self.misses += 1
        return self.fallback.iv(on, spot, strike, dte, right)

    @property
    def coverage(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


def _bisect_date(dates: Sequence[dt.date], target: dt.date) -> int:
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return min(lo, len(dates) - 1)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

@dataclass
class BacktestRules:
    """
    Entry, management, and exit logic.

    Defaults are the conventional premium-selling discipline -- 45 DTE entry,
    close at 50% of credit or 21 DTE -- because that is the ruleset most people
    want to test first, not because it is endorsed.
    """

    template_id: str = "short_strangle"
    params: dict = field(default_factory=dict)

    entry_every_days: int = 7
    target_dte: int = 45
    contracts: int = 1
    max_concurrent: int = 1

    # Both are fractions of the entry cost basis: credit collected for credit
    # structures, debit paid for debit structures.
    profit_target_pct: float | None = 0.50
    stop_loss_pct: float | None = 2.00

    close_at_dte: int | None = 21
    skip_earnings: bool = False

    commission_per_contract: float = 0.65
    slippage_pct: float = 0.01
    variance_risk_premium: float = 1.15

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    entry_date: dt.date
    exit_date: dt.date
    entry_spot: float
    exit_spot: float
    entry_basis: float          # positive = debit paid, negative = credit taken
    pl: float
    exit_reason: str
    days_held: int
    dte_at_entry: float
    dte_at_exit: float
    legs: list[dict]
    commission: float
    max_adverse: float
    max_favorable: float

    @property
    def is_win(self) -> bool:
        return self.pl > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_date"] = self.entry_date.isoformat()
        d["exit_date"] = self.exit_date.isoformat()
        d["is_win"] = self.is_win
        d["return_on_basis"] = (self.pl / abs(self.entry_basis)
                                if abs(self.entry_basis) > 1e-9 else None)
        return d


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class _OpenTrade:
    """
    An open position plus its clock.

    We track *days held* rather than a single expiry date because a calendar or
    diagonal has legs on different expirations. Each leg ages on its own
    schedule; the position's near expiry is what the exit rules key off.
    """

    __slots__ = ("position", "entry_date", "entry_spot", "basis", "near_dte",
                 "worst", "best")

    def __init__(self, position: Position, entry_date: dt.date, entry_spot: float,
                 basis: float):
        self.position = position
        self.entry_date = entry_date
        self.entry_spot = entry_spot
        self.basis = basis
        self.near_dte = position.min_dte      # days from entry to first expiry
        self.worst = 0.0
        self.best = 0.0

    def days_held(self, today: dt.date) -> int:
        return (today - self.entry_date).days

    def dte_left(self, today: dt.date) -> float:
        return self.near_dte - self.days_held(today)

    @property
    def expiry_date(self) -> dt.date:
        return self.entry_date + dt.timedelta(days=int(self.near_dte))


def run_backtest(bars: Sequence, rules: BacktestRules, *,
                 quote_source: QuoteSource | None = None,
                 rate: float = 0.045,
                 symbol: str = "UNDL",
                 earnings_dates: Sequence[dt.date] = (),
                 warmup_days: int = 60) -> dict:
    """
    Replay `rules` over `bars`.

    `bars` must be ascending daily bars with `.t` (datetime) and `.c` (close).
    Returns the full result block: trades, equity curve, metrics, and the
    provenance of the prices used.
    """
    if len(bars) < warmup_days + 30:
        raise ValueError(
            f"need at least {warmup_days + 30} bars to backtest; got {len(bars)}")

    dates = [b.t.date() if hasattr(b.t, "date") else b.t for b in bars]
    closes = [b.c for b in bars]
    closes_by_date = dict(zip(dates, closes))

    source = quote_source or ModelledQuoteSource(
        closes_by_date=closes_by_date, ordered_dates=dates,
        variance_risk_premium=rules.variance_risk_premium)

    template = get_template(rules.template_id)
    earnings = set(earnings_dates)

    open_trades: list[_OpenTrade] = []
    closed: list[Trade] = []
    equity: list[dict] = []

    realized = 0.0
    last_entry: dt.date | None = None
    skipped_for_earnings = 0

    for i in range(warmup_days, len(dates)):
        today = dates[i]
        spot = closes[i]

        # ---- mark and manage what is open -------------------------------
        still_open: list[_OpenTrade] = []
        for trade in open_trades:
            days_held = trade.days_held(today)
            dte_left = trade.dte_left(today)

            value = _position_value(trade.position, spot, today, days_held,
                                    source, rate, rules)
            pl = value - trade.basis - trade.position.total_commission
            trade.worst = min(trade.worst, pl)
            trade.best = max(trade.best, pl)

            reason = _exit_reason(pl, trade.basis, dte_left, rules)
            if reason is None:
                still_open.append(trade)
                continue

            # Exiting costs the other half of the commission and slippage.
            exit_commission = trade.position.total_commission
            pl -= exit_commission
            realized += pl
            closed.append(Trade(
                entry_date=trade.entry_date, exit_date=today,
                entry_spot=trade.entry_spot, exit_spot=spot,
                entry_basis=trade.basis, pl=pl, exit_reason=reason,
                days_held=days_held,
                dte_at_entry=trade.near_dte,
                dte_at_exit=max(dte_left, 0),
                legs=[{"kind": l.kind, "strike": l.strike, "quantity": l.quantity,
                       "dte": l.dte, "entry_price": round(l.entry_price, 4)}
                      for l in trade.position.legs],
                commission=trade.position.total_commission + exit_commission,
                max_adverse=trade.worst, max_favorable=trade.best,
            ))
        open_trades = still_open

        # ---- open a new position ----------------------------------------
        can_enter = (
            len(open_trades) < rules.max_concurrent
            and (last_entry is None
                 or (today - last_entry).days >= rules.entry_every_days)
        )
        if can_enter:
            opened = _open_position(template, rules, spot, today, source,
                                    rate, symbol)
            if opened is not None:
                position, basis = opened
                # The horizon that matters is the position's own longest leg --
                # a diagonal holding a LEAPS is exposed to every announcement
                # until that LEAPS expires, not just the front one.
                horizon = today + dt.timedelta(days=int(position.max_dte))
                if rules.skip_earnings and any(today <= e <= horizon for e in earnings):
                    skipped_for_earnings += 1
                else:
                    open_trades.append(_OpenTrade(position, today, spot, basis))
                    last_entry = today

        # ---- equity ------------------------------------------------------
        unrealized = 0.0
        for trade in open_trades:
            value = _position_value(trade.position, spot, today,
                                    trade.days_held(today), source, rate, rules)
            unrealized += value - trade.basis - trade.position.total_commission

        equity.append({
            "date": today.isoformat(),
            "spot": round(spot, 4),
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
            "equity": round(realized + unrealized, 2),
            "open_positions": len(open_trades),
        })

    # ---- force-close anything still open at the end ---------------------
    if open_trades:
        today = dates[-1]
        spot = closes[-1]
        for trade in open_trades:
            dte_left = trade.dte_left(today)
            value = _position_value(trade.position, spot, today,
                                    trade.days_held(today), source, rate, rules)
            pl = (value - trade.basis - 2 * trade.position.total_commission)
            realized += pl
            closed.append(Trade(
                entry_date=trade.entry_date, exit_date=today,
                entry_spot=trade.entry_spot, exit_spot=spot,
                entry_basis=trade.basis, pl=pl,
                exit_reason="end of backtest",
                days_held=(today - trade.entry_date).days,
                dte_at_entry=trade.near_dte,
                dte_at_exit=max(dte_left, 0),
                legs=[{"kind": l.kind, "strike": l.strike, "quantity": l.quantity,
                       "dte": l.dte, "entry_price": round(l.entry_price, 4)}
                      for l in trade.position.legs],
                commission=2 * trade.position.total_commission,
                max_adverse=trade.worst, max_favorable=trade.best,
            ))

    metrics = compute_metrics(closed, equity)

    warnings: list[str] = []
    if closed:
        entry_dte = closed[0].dte_at_entry
        if (rules.close_at_dte is not None and rules.close_at_dte >= entry_dte
                and metrics["average_days_held"] is not None
                and metrics["average_days_held"] < 3):
            warnings.append(
                f"Every trade exited almost immediately: the close-at-{rules.close_at_dte}-DTE "
                f"rule fires on day one because this structure's nearest leg opens "
                f"at {entry_dte:.0f} DTE. Raise the target DTE or lower the exit "
                f"threshold -- the results below are an artefact of the rules, not "
                f"of the strategy.")
    if not closed:
        warnings.append(
            "No trades were opened. Check that the entry cadence and date range "
            "leave room for at least one position.")

    caveat = (
        "Prices are modelled, not historical quotes. Implied volatility is "
        "reconstructed as trailing realized volatility times a "
        f"{rules.variance_risk_premium:.2f} variance risk premium, with a "
        "parametric skew. This tests the ENTRY, MANAGEMENT AND EXIT RULES "
        "against real price paths -- it is not a record of fills. It cannot "
        "reproduce volatility crushes, skew shifts, or the spread you actually "
        "pay, which is exactly where short-premium results are won and lost. "
        "Populate the option_quotes table and re-run for a true P&L backtest."
        if not source.is_historical else
        "Priced from stored historical option quotes."
    )

    return {
        "symbol": symbol,
        "template": rules.template_id,
        "template_name": template.name,
        "rules": rules.to_dict(),
        "start": dates[warmup_days].isoformat(),
        "end": dates[-1].isoformat(),
        "trading_days": len(dates) - warmup_days,
        "quote_source": source.name,
        "is_historical_pricing": source.is_historical,
        "quote_coverage": round(getattr(source, "coverage", 0.0), 4)
        if source.is_historical else None,
        "caveat": caveat,
        "skipped_for_earnings": skipped_for_earnings,
        "warnings": warnings,
        "trades": [t.to_dict() for t in closed],
        "equity_curve": equity,
        "metrics": metrics,
    }


def _open_position(template, rules: BacktestRules, spot: float, today: dt.date,
                   source: QuoteSource, rate: float,
                   symbol: str) -> tuple[Position, float] | None:
    """Build the structure at historical prices. Returns (position, cost basis)."""
    surface = _SourceSurface(source, today, spot)
    ctx = MarketContext(
        spot=spot, surface=surface, rate=rate, dividend_yield=0.0,
        strike_increment=_increment(spot), american=False, symbol=symbol,
        slippage_pct=rules.slippage_pct,
    )
    params = _apply_target_dte(template, dict(rules.params), rules.target_dte)
    params["quantity"] = rules.contracts

    try:
        position = template.make(ctx, params)
    except Exception:                                          # noqa: BLE001
        return None

    position.commission_per_contract = rules.commission_per_contract

    gross = sum(l.quantity * l.entry_price * l.multiplier for l in position.legs)
    return position, gross


def _apply_target_dte(template, params: dict, target_dte: int) -> dict:
    """
    Push the requested DTE onto whatever the template calls it.

    Most templates take `dte`. Calendars and diagonals take `front_dte` and
    `back_dte`, and PMCC takes `long_dte`/`short_dte`. Blindly setting `dte`
    would leave those structures on their built-in defaults while the UI showed
    the user a target they had chosen -- silently backtesting something other
    than what was asked for. The spacing between a template's own defaults is
    preserved so the *shape* of the structure survives the shift.
    """
    keys = {p.key for p in template.params}

    if "dte" in keys and "dte" not in params:
        params["dte"] = target_dte
        return params

    defaults = {p.key: p.default for p in template.params}
    front_keys = [k for k in ("front_dte", "short_dte") if k in keys]
    back_keys = [k for k in ("back_dte", "long_dte") if k in keys]

    if front_keys:
        front = front_keys[0]
        base = float(defaults.get(front, target_dte)) or target_dte
        shift = target_dte - base
        if front not in params:
            params[front] = max(int(base + shift), 1)
        for back in back_keys:
            back_base = float(defaults.get(back, base * 2))
            if back not in params:
                params[back] = max(int(back_base + shift), int(params[front]) + 7)
    return params


class _SourceSurface:
    """Adapts a QuoteSource to the VolSurface interface the templates expect."""

    def __init__(self, source: QuoteSource, on: dt.date, spot: float):
        self.source = source
        self.on = on
        self.spot = spot

    def iv(self, strike: float, dte: float, kind: str = "call") -> float:
        return self.source.iv(self.on, self.spot, strike, dte, kind) or 0.30


def _increment(spot: float) -> float:
    if spot < 25:
        return 0.5
    if spot < 100:
        return 1.0
    if spot < 250:
        return 2.5
    if spot < 1000:
        return 5.0
    return 10.0


def _position_value(position: Position, spot: float, today: dt.date,
                    days_held: float, source: QuoteSource, rate: float,
                    rules: BacktestRules) -> float:
    """
    Gross mark of the position, in dollars, at today's spot.

    Each leg's remaining life is its own entry DTE minus days held. Using one
    shared DTE for every leg would value a calendar's back month as though it
    expired with the front -- turning a tent-shaped payoff into nonsense.
    """
    total = 0.0
    for leg in position.legs:
        if leg.kind == "stock":
            total += leg.quantity * spot
            continue
        remaining = max(leg.dte - days_held, 0.0)
        if remaining <= 0:
            value = intrinsic(spot, float(leg.strike), leg.kind)
        else:
            iv = source.iv(today, spot, float(leg.strike), remaining, leg.kind) or 0.30
            value = bs_price(spot, float(leg.strike), years_from_dte(remaining),
                             rate, 0.0, iv, leg.kind)
            # Closing costs the spread too, and it always works against you.
            value *= (1.0 - rules.slippage_pct) if leg.quantity > 0 else \
                     (1.0 + rules.slippage_pct)
        total += leg.quantity * value * leg.multiplier
    return total


def _exit_reason(pl: float, basis: float, dte_left: float,
                 rules: BacktestRules) -> str | None:
    if dte_left <= 0:
        return "expired"

    risk_capital = abs(basis)
    if risk_capital > 1e-9:
        if rules.profit_target_pct is not None and pl >= rules.profit_target_pct * risk_capital:
            return f"profit target ({rules.profit_target_pct:.0%})"
        if rules.stop_loss_pct is not None and pl <= -rules.stop_loss_pct * risk_capital:
            return f"stop loss ({rules.stop_loss_pct:.1f}x)"

    if rules.close_at_dte is not None and dte_left <= rules.close_at_dte:
        return f"{rules.close_at_dte} DTE"
    return None


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def compute_metrics(trades: Sequence[Trade], equity: Sequence[dict]) -> dict:
    """
    Trade and equity statistics.

    Drawdown is measured on the equity curve including open positions, not on
    closed trades alone. Closed-trade drawdown is the flattering version: it
    hides the fact that a position was down 4x the credit before it recovered,
    which is the excursion that actually forces people out of trades.
    """
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": None,
            "total_pl": 0.0, "average_pl": None, "profit_factor": None,
            "expectancy": None, "max_drawdown": 0.0, "max_drawdown_pct": None,
            "sharpe": None, "average_days_held": None,
            "largest_win": None, "largest_loss": None,
            "average_win": None, "average_loss": None,
            "max_consecutive_losses": 0, "worst_excursion": 0.0,
            "exposure_pct": None,
        }

    pls = [t.pl for t in trades]
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    peak = 0.0
    max_dd = 0.0
    for row in equity:
        eq = row["equity"]
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)

    # Daily equity changes -> Sharpe. Zero-variance runs return None rather
    # than an infinite ratio.
    daily = [equity[i]["equity"] - equity[i - 1]["equity"]
             for i in range(1, len(equity))]
    sharpe = None
    if len(daily) > 5:
        mean = sum(daily) / len(daily)
        var = sum((d - mean) ** 2 for d in daily) / (len(daily) - 1)
        sd = math.sqrt(var)
        if sd > 1e-9:
            sharpe = (mean / sd) * math.sqrt(252)

    streak = worst_streak = 0
    for t in trades:
        if t.pl <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    exposed_days = sum(1 for row in equity if row["open_positions"] > 0)

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades),
        "total_pl": round(sum(pls), 2),
        "average_pl": round(sum(pls) / len(pls), 2),
        "average_win": round(sum(wins) / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "largest_win": round(max(pls), 2),
        "largest_loss": round(min(pls), 2),
        # Profit factor is undefined, not infinite, when nothing lost money.
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 1e-9 else None,
        "expectancy": round(sum(pls) / len(pls), 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": (round(max_dd / peak, 4) if peak > 1e-9 else None),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "average_days_held": round(sum(t.days_held for t in trades) / len(trades), 1),
        "max_consecutive_losses": worst_streak,
        "worst_excursion": round(min((t.max_adverse for t in trades), default=0.0), 2),
        "exposure_pct": round(exposed_days / len(equity), 4) if equity else None,
    }


def pl_distribution(trades: Sequence[dict], buckets: int = 12) -> dict:
    """Histogram of trade P/L, for the distribution chart."""
    if not trades:
        return {"edges": [], "counts": [], "min": 0, "max": 0}
    pls = [t["pl"] for t in trades]
    lo, hi = min(pls), max(pls)
    if abs(hi - lo) < 1e-9:
        return {"edges": [lo, hi], "counts": [len(pls)], "min": lo, "max": hi}

    step = (hi - lo) / buckets
    edges = [lo + i * step for i in range(buckets + 1)]
    counts = [0] * buckets
    for p in pls:
        idx = min(int((p - lo) / step), buckets - 1)
        counts[idx] += 1
    return {"edges": edges, "counts": counts, "min": lo, "max": hi}
