"""
Order-flow footprint: bars built from individual trades, and a POC-defence
strategy backtested on the same trades.

What a footprint is
-------------------
A candle says where price went in a minute. A footprint says *where inside the
candle the business was done*: for every price row it holds the volume that hit
the bid (aggressive sellers) and the volume that lifted the ask (aggressive
buyers). From that come the numbers this module is built around:

* **POC** (point of control) -- the row with the most volume. The single
  highest-transaction price of the bar.
* **Value area** -- the contiguous rows around the POC holding 70% of volume.
* **Delta** -- ask volume minus bid volume. Net aggression.
* **Diagonal imbalance** -- ask volume at a row against bid volume one row
  below (and the mirror). Aggressors overwhelming the passive side.
* **High-volume nodes** -- rows trading a multiple of the bar's average row.

The strategy ("POC defence")
----------------------------
A bar qualifies when it is a high-transaction bar (volume a multiple of the
recent median) whose POC sits at one end of its range and which closed away
from it. Read literally: most of the bar's business was done low, and price
left upward. Whoever was buying there absorbed the selling. The entry is a
resting limit *at that POC* -- we only trade if price comes back to the
high-transaction area -- with the stop beyond the bar's extreme, a partial exit
at the first target and the remainder run to the second.

What this module refuses to do
------------------------------
Three things make most footprint backtests worthless, and each is handled
structurally rather than by caveat:

1. **Lookahead.** A signal is computed from a bar only after its last trade,
   and its order can fill only on trades *after* that. There is no path by
   which a bar's own trades fill its own signal.

2. **Intrabar guessing.** OHLC backtests have to guess whether the stop or the
   target came first inside a bar. This one never guesses: orders are walked
   through the actual trade sequence, in order.

3. **Free limit fills.** A limit order at a price that merely *touched* is not
   a fill -- you were in a queue behind everyone already resting there. By
   default a limit fills only when a trade prints *through* it.

And one thing no code can fix: **a win rate is mostly a property of the exit
geometry, not of the signal.** Taking half off at 1R and moving the stop to
breakeven produces a high win rate on coin-flip entries. So every backtest here
is reported next to a control -- the same order mechanics on randomly chosen
bars and directions -- and the question it answers is the only one that
matters: is the signal better than no signal?
"""

from __future__ import annotations

import csv
import io
import math
import random
from dataclasses import asdict, dataclass, field

import numpy as np

VALUE_AREA_FRACTION = 0.70


# --------------------------------------------------------------------------
# Trades
# --------------------------------------------------------------------------

@dataclass
class TradeTape:
    """
    A sequence of trades in time order, as parallel arrays.

    `side` is the aggressor: +1 a buyer lifted the ask, -1 a seller hit the
    bid. `classified` records how that was obtained, because it decides how far
    the bid/ask split can be trusted:

    * "exchange"  -- the venue reported the aggressor (futures, crypto). Exact.
    * "tick_rule" -- inferred from price changes. US equity trade feeds carry no
      aggressor flag, so this is the usual case for stocks, and it misclassifies
      a meaningful share of prints (commonly cited at 10-25%). Delta and
      imbalance built on it are estimates.
    * "generated" -- a synthetic tape.
    """
    ts: np.ndarray            # float64 epoch seconds, non-decreasing
    price: np.ndarray         # float64
    size: np.ndarray          # float64
    side: np.ndarray          # int8: +1 buy aggressor, -1 sell aggressor
    classified: str = "exchange"
    source: str = ""

    def __len__(self) -> int:
        return int(self.ts.shape[0])


def tick_rule(price: np.ndarray) -> np.ndarray:
    """
    Classify aggressor side from price changes alone.

    An uptick is a buy, a downtick is a sell, and a trade at an unchanged price
    inherits the last known direction (the "zero-tick" rule). The first trades,
    before any price change, have no information and are assigned +1/-1
    alternately so they cancel in delta rather than bias it.
    """
    n = price.shape[0]
    side = np.zeros(n, dtype=np.int8)
    if n == 0:
        return side
    diff = np.sign(np.diff(price))
    side[1:] = diff.astype(np.int8)
    # Forward-fill zeros with the previous non-zero direction.
    idx = np.where(side != 0, np.arange(n), 0)
    np.maximum.accumulate(idx, out=idx)
    filled = side[idx]
    lead = filled == 0                      # before the first price change
    filled[lead] = np.where(np.arange(int(lead.sum())) % 2 == 0, 1, -1)
    return filled.astype(np.int8)


_TIME_KEYS = ("timestamp", "time", "ts", "t", "date", "datetime", "transact_time",
              "sip_timestamp")
_PRICE_KEYS = ("price", "p", "px", "last")
_SIZE_KEYS = ("size", "qty", "quantity", "volume", "s", "q", "amount")
_SIDE_KEYS = ("side", "aggressor", "is_buyer_maker", "m", "taker_side")


def _epoch_seconds(raw: str) -> float:
    """Seconds, milliseconds, microseconds or nanoseconds since epoch, or ISO-8601."""
    raw = raw.strip()
    try:
        v = float(raw)
    except ValueError:
        import datetime as dt                                      # noqa: PLC0415
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    # Magnitude tells the unit: 1e9 s, 1e12 ms, 1e15 us, 1e18 ns (all ~2001-2286).
    for scale in (1e9, 1e6, 1e3):
        if abs(v) >= 1e9 * scale * 0.1:
            return v / scale
    return v


def _side_value(raw: str, key: str) -> int:
    v = raw.strip().lower()
    if key in ("is_buyer_maker", "m"):
        # Binance convention: buyer is the maker => the SELLER was the aggressor.
        return -1 if v in ("true", "1", "t", "yes") else 1
    if v in ("buy", "b", "bid_lift", "1", "+1", "ask", "a", "true"):
        return 1
    if v in ("sell", "s", "-1", "bid", "false"):
        return -1
    return 0


def parse_trades_csv(text: str, *, source: str = "upload") -> TradeTape:
    """
    Read a trade tape from CSV.

    Accepts a header row naming time, price, size and optionally side, under the
    column names common exports use. A headerless file is read as Binance
    aggTrades (id, price, qty, first_id, last_id, time, is_buyer_maker, ...),
    since that is the most available free source of aggressor-flagged trades.

    With no side column the aggressor is inferred by the tick rule and the tape
    is marked accordingly.
    """
    rows = [r for r in csv.reader(io.StringIO(text.strip())) if r and any(c.strip() for c in r)]
    if not rows:
        raise ValueError("no rows in the uploaded tape")

    head = [c.strip().lower() for c in rows[0]]

    def is_number(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    if all(is_number(c) or c in ("true", "false") for c in head):
        if len(head) < 7:
            raise ValueError(
                "headerless CSV is read as Binance aggTrades and needs at least "
                "7 columns; add a header row naming time, price, size[, side]")
        cols = {"price": 1, "size": 2, "time": 5, "side": 6}
        side_key = "is_buyer_maker"
        body = rows
    else:
        def find(keys):
            return next((head.index(k) for k in keys if k in head), None)
        cols = {"time": find(_TIME_KEYS), "price": find(_PRICE_KEYS),
                "size": find(_SIZE_KEYS), "side": find(_SIDE_KEYS)}
        missing = [k for k in ("time", "price", "size") if cols[k] is None]
        if missing:
            raise ValueError(f"CSV header is missing a column for: {', '.join(missing)}. "
                             f"Found columns: {', '.join(head)}")
        side_key = head[cols["side"]] if cols["side"] is not None else ""
        body = rows[1:]

    ts, px, sz, sd = [], [], [], []
    for n, r in enumerate(body, start=1):
        try:
            t = _epoch_seconds(r[cols["time"]])
            p = float(r[cols["price"]])
            q = float(r[cols["size"]])
        except (ValueError, IndexError) as exc:
            raise ValueError(f"row {n}: could not read time/price/size ({exc})") from exc
        if p <= 0 or q <= 0:
            continue
        ts.append(t)
        px.append(p)
        sz.append(q)
        if cols["side"] is not None:
            sd.append(_side_value(r[cols["side"]], side_key))

    if len(ts) < 2:
        raise ValueError("the tape needs at least two valid trades")

    order = np.argsort(np.asarray(ts), kind="stable")
    price = np.asarray(px, dtype=np.float64)[order]
    have_side = cols["side"] is not None and all(s != 0 for s in sd)
    side = (np.asarray(sd, dtype=np.int8)[order] if have_side else tick_rule(price))
    return TradeTape(ts=np.asarray(ts, dtype=np.float64)[order], price=price,
                     size=np.asarray(sz, dtype=np.float64)[order], side=side,
                     classified="exchange" if have_side else "tick_rule",
                     source=source)


# --------------------------------------------------------------------------
# Synthetic tape
# --------------------------------------------------------------------------

SYNTHETIC_MASTER_SESSIONS = 10


def synthetic_tape(seed: int, *, sessions: int = 3, start_price: float = 100.0,
                   tick_size: float = 0.01, annual_vol: float = 0.30,
                   trades_per_minute: float = 140.0, session_minutes: int = 390,
                   end_epoch: float | None = None) -> TradeTape:
    """
    A generated trade tape with realistic texture and **no edge**.

    Texture it does have, because a footprint drawn from it should look like
    one: a U-shaped intraday activity curve, clustered bursts of volume,
    lognormal trade sizes, a one-tick bid/ask bounce, and aggressor flow that
    leans with the price move in progress (so delta agrees with bar direction,
    as it does on real tapes).

    What it deliberately lacks: any dependence of *future* price on anything
    observable. The mid is a random walk whose increments are independent of
    every past trade. Order flow reads the past move; the next move does not
    read order flow. Consequently no strategy has positive expectancy on this
    tape before costs, and after costs every strategy loses. A backtest that
    shows a profit here has a bug -- which is exactly how the test-suite uses it.

    This is a rig for checking the machinery and seeing the chart. It is not a
    market, and nothing measured on it says anything about one.

    The tape is always generated at a fixed master length and the most recent
    `sessions` are sliced from it, so asking for 2 sessions and asking for 5
    return the same trades on the days they share. (The daily-bar generator
    once got this wrong; see tests/test_providers_stability.py.)
    """
    sessions = max(1, min(int(sessions), SYNTHETIC_MASTER_SESSIONS))
    keep = sessions
    sessions = SYNTHETIC_MASTER_SESSIONS
    rng = np.random.default_rng(seed)
    if end_epoch is None:
        end_epoch = 1_700_000_000.0             # fixed anchor: deterministic by default
    day = 86400.0
    last_open = math.floor(end_epoch / day) * day + 14.5 * 3600      # 14:30 UTC open

    # Per-trade volatility so that a session's worth of trades matches the
    # requested annualised figure.
    per_session = annual_vol / math.sqrt(252.0)
    n_expected = trades_per_minute * session_minutes
    sigma_trade = start_price * per_session / math.sqrt(n_expected)

    ts_parts, px_parts, sz_parts, sd_parts = [], [], [], []
    price = start_price
    minutes = np.arange(session_minutes)
    # U-shape: busy open and close, quiet lunch.
    u = 0.55 + 1.6 * ((minutes / (session_minutes - 1)) - 0.5) ** 2 * 4 * 0.45 \
        + 0.9 * np.exp(-minutes / 25.0)

    for s in range(sessions):
        open_t = last_open - (sessions - 1 - s) * day
        # Clustered intensity: an AR(1) in logs, so busy minutes come in runs.
        z = np.zeros(session_minutes)
        shock = rng.normal(0, 0.45, session_minutes)
        for m in range(1, session_minutes):
            z[m] = 0.80 * z[m - 1] + shock[m]
        counts = rng.poisson(np.maximum(trades_per_minute * u * np.exp(z - 0.25), 3.0))
        n = int(counts.sum())

        minute_of = np.repeat(minutes, counts)
        t = open_t + minute_of * 60.0 + rng.uniform(0, 60.0, n)
        order = np.argsort(t, kind="stable")
        t = t[order]

        # Efficient price: a pure random walk. Nothing below feeds back into it.
        mid = price + np.cumsum(rng.normal(0, sigma_trade, n))
        mid = np.maximum(mid, tick_size * 10)

        # Aggressor leans with the move over the last ~40 trades (the past).
        look = 40
        past = np.empty(n)
        past[:look] = mid[:look] - price
        past[look:] = mid[look:] - mid[:-look]
        lean = past / (sigma_trade * math.sqrt(look))
        p_buy = 1.0 / (1.0 + np.exp(-0.9 * lean))
        side = np.where(rng.uniform(0, 1, n) < p_buy, 1, -1).astype(np.int8)

        # Buys print at the ask, sells at the bid: one tick apart around the mid.
        bid = np.floor(mid / tick_size)
        px = (bid + (side > 0)) * tick_size

        size = np.maximum(np.round(rng.lognormal(3.6, 1.05, n)), 1.0)

        ts_parts.append(t)
        px_parts.append(np.round(px, 8))
        sz_parts.append(size)
        sd_parts.append(side)
        price = float(mid[-1])

    return TradeTape(ts=np.concatenate(ts_parts[-keep:]),
                     price=np.concatenate(px_parts[-keep:]),
                     size=np.concatenate(sz_parts[-keep:]),
                     side=np.concatenate(sd_parts[-keep:]),
                     classified="generated", source="synthetic")


# --------------------------------------------------------------------------
# Footprint bars
# --------------------------------------------------------------------------

@dataclass
class FootprintBar:
    t: int                      # bar open, epoch seconds
    o: float
    h: float
    l: float
    c: float
    volume: float
    delta: float                # ask volume - bid volume
    cum_delta: float            # running delta within the session
    poc: float                  # price of the highest-volume row
    poc_volume: float
    poc_pos: float              # 0 = POC at the bar low, 1 = at the high
    vah: float                  # value-area high
    val: float                  # value-area low
    vwap: float                 # session VWAP at this bar's close
    buy_imbalances: int         # rows where ask overwhelms the bid below
    sell_imbalances: int
    buy_stack: int              # longest run of consecutive buy-imbalance rows
    sell_stack: int
    rows: list                  # [[price, bid_vol, ask_vol, flag], ...] high -> low
    session: int                # session index (epoch day)
    i0: int = 0                 # first trade index (inclusive)
    i1: int = 0                 # last trade index (exclusive)

    def to_dict(self, *, with_rows: bool = True) -> dict:
        d = {k: getattr(self, k) for k in (
            "t", "o", "h", "l", "c", "volume", "delta", "cum_delta", "poc",
            "poc_volume", "poc_pos", "vah", "val", "vwap", "buy_imbalances",
            "sell_imbalances", "buy_stack", "sell_stack")}
        if with_rows:
            d["rows"] = self.rows
        return d


def _longest_run(flags: list[bool]) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def build_footprint(tape: TradeTape, *, row_size: float, bar_seconds: int = 60,
                    imbalance_ratio: float = 3.0, imbalance_min_volume: float = 0.0,
                    session_gap_seconds: float = 3600.0) -> list[FootprintBar]:
    """
    Aggregate a trade tape into footprint bars.

    `row_size` is the height of one footprint row in price units -- the tick
    size, or a multiple of it where a one-tick ladder would be too fine to read
    (a $500 stock moving 80 cents in a minute wants 5-cent rows, not 80 rows).

    A bar exists only where trades exist; empty minutes produce no bar rather
    than a fabricated one. A gap between consecutive trades longer than
    `session_gap_seconds` starts a new session, which resets cumulative delta
    and VWAP -- carrying either across an overnight gap makes both meaningless.

    Row flags: 1 = buy imbalance, -1 = sell imbalance, 0 = neither. An
    imbalance is diagonal, as on every footprint platform: the ask volume at a
    row is compared with the bid volume one row *below*, because those are the
    two sides of the same spread.
    """
    if row_size <= 0:
        raise ValueError("row_size must be positive")
    n = len(tape)
    if n == 0:
        return []

    bar_id = np.floor(tape.ts / bar_seconds).astype(np.int64)
    row_id = np.floor(tape.price / row_size + 1e-9).astype(np.int64)
    # Boundaries of runs of equal bar_id. The tape is time-ordered.
    cuts = np.flatnonzero(np.diff(bar_id)) + 1
    starts = np.concatenate(([0], cuts))
    ends = np.concatenate((cuts, [n]))

    bars: list[FootprintBar] = []
    session = 0
    cum_delta = 0.0
    pv = vv = 0.0
    prev_last_ts: float | None = None

    for a, b in zip(starts.tolist(), ends.tolist()):
        if prev_last_ts is not None and tape.ts[a] - prev_last_ts > session_gap_seconds:
            session += 1
            cum_delta = 0.0
            pv = vv = 0.0
        prev_last_ts = float(tape.ts[b - 1])

        rid = row_id[a:b]
        sz = tape.size[a:b]
        sd = tape.side[a:b]
        px = tape.price[a:b]

        lo_row, hi_row = int(rid.min()), int(rid.max())
        span = hi_row - lo_row + 1
        off = rid - lo_row
        ask = np.bincount(off, weights=np.where(sd > 0, sz, 0.0), minlength=span)
        bid = np.bincount(off, weights=np.where(sd < 0, sz, 0.0), minlength=span)
        total = ask + bid
        volume = float(total.sum())

        # POC: highest-volume row; ties go to the row nearest the bar's middle,
        # which is the convention that does not bias poc_pos toward an extreme.
        top = np.flatnonzero(total == total.max())
        poc_i = int(top[np.argmin(np.abs(top - (span - 1) / 2.0))])

        # Value area: grow outward from the POC, taking the larger neighbour.
        lo_i = hi_i = poc_i
        acc = float(total[poc_i])
        target = VALUE_AREA_FRACTION * volume
        while acc < target and (lo_i > 0 or hi_i < span - 1):
            up = total[hi_i + 1] if hi_i < span - 1 else -1.0
            dn = total[lo_i - 1] if lo_i > 0 else -1.0
            if up >= dn:
                hi_i += 1
                acc += float(up)
            else:
                lo_i -= 1
                acc += float(dn)

        # Diagonal imbalances. The floor keeps a 3-lot against a 1-lot at the
        # thin edge of a bar from counting: the dominant side must itself be a
        # meaningful share of an average row.
        floor = max(imbalance_min_volume, 0.25 * volume / span)
        buy_f = [False] * span
        sell_f = [False] * span
        for i in range(span):
            if i > 0 and ask[i] >= floor and ask[i] >= imbalance_ratio * bid[i - 1]:
                buy_f[i] = True
            if i < span - 1 and bid[i] >= floor and bid[i] >= imbalance_ratio * ask[i + 1]:
                sell_f[i] = True
        # A row cannot be both; the larger side wins.
        for i in range(span):
            if buy_f[i] and sell_f[i]:
                if ask[i] >= bid[i]:
                    sell_f[i] = False
                else:
                    buy_f[i] = False

        delta = float(ask.sum() - bid.sum())
        cum_delta += delta
        pv += float((px * sz).sum())
        vv += volume

        # A row's price is the bottom of its bucket; trades inside the row are
        # at or above it. The POC is reported as the row's midpoint so that an
        # order "at the POC" sits inside the high-volume row rather than on its
        # lower edge.
        def row_price(i: int) -> float:
            return round((lo_row + i) * row_size, 8)

        poc_price = round(row_price(poc_i) + row_size / 2.0, 8)
        h, l = float(px.max()), float(px.min())
        poc_clamped = min(max(poc_price, l), h)
        poc_pos = 0.5 if h == l else (poc_clamped - l) / (h - l)

        rows = [[row_price(i), float(bid[i]), float(ask[i]),
                 1 if buy_f[i] else (-1 if sell_f[i] else 0)]
                for i in range(span - 1, -1, -1)]

        bars.append(FootprintBar(
            t=int(bar_id[a]) * bar_seconds,
            o=float(px[0]), h=h, l=l, c=float(px[-1]),
            volume=volume, delta=delta, cum_delta=cum_delta,
            poc=poc_clamped, poc_volume=float(total[poc_i]), poc_pos=float(poc_pos),
            vah=round(row_price(hi_i) + row_size, 8), val=row_price(lo_i),
            vwap=pv / vv if vv else float(px[-1]),
            buy_imbalances=sum(buy_f), sell_imbalances=sum(sell_f),
            buy_stack=_longest_run(buy_f), sell_stack=_longest_run(sell_f),
            rows=rows, session=session, i0=a, i1=b))
    return bars


def suggest_row_size(tape: TradeTape, tick_size: float, *, bar_seconds: int = 60,
                     target_rows: int = 12) -> float:
    """
    A row height that gives a typical bar roughly `target_rows` rows.

    Too fine and every row holds a handful of shares, so the POC is noise; too
    coarse and the bar is three rows and the POC carries no location. A dozen
    rows is where footprint platforms' own auto-settings tend to land.
    """
    if len(tape) == 0:
        return tick_size
    bar_id = np.floor(tape.ts / bar_seconds).astype(np.int64)
    cuts = np.flatnonzero(np.diff(bar_id)) + 1
    starts = np.concatenate(([0], cuts))
    hi = np.maximum.reduceat(tape.price, starts)
    lo = np.minimum.reduceat(tape.price, starts)
    typical = float(np.median(hi - lo))
    if typical <= 0:
        return tick_size
    raw = typical / target_rows / tick_size
    for mult in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if mult >= raw:
            return round(mult * tick_size, 8)
    return round(1000 * tick_size, 8)


# --------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------

@dataclass
class StrategyParams:
    """
    Every knob of the POC-defence strategy. Defaults are a reasonable starting
    point, not a tuned result -- there is no tuned result without real data.
    """
    # --- the setup: what makes a bar a signal ---------------------------
    volume_mult: float = 1.5        # bar volume >= this x the rolling median
    volume_lookback: int = 20       # bars in that median
    poc_zone: float = 0.35          # POC within this fraction of the bar's low (long) / high (short)
    require_delta: bool = True      # net aggression must agree with the direction
    min_stack: int = 0              # stacked imbalance rows required in the direction (0 = off)
    trend_filter: bool = False      # longs only above session VWAP, shorts only below
    allow_long: bool = True
    allow_short: bool = True
    min_range_ticks: float = 4.0    # ignore bars too narrow to have a POC location

    # --- the entry -------------------------------------------------------
    entry_ttl_bars: int = 3         # cancel the resting limit after this many bars
    fill_through: bool = True       # limit fills only when price trades THROUGH it

    # --- the exits -------------------------------------------------------
    stop_buffer_ticks: float = 2.0  # stop this far beyond the signal bar's extreme
    min_risk_ticks: float = 4.0     # widen the stop to at least this far from entry
    target1_r: float = 1.0          # first target, in multiples of initial risk
    target2_r: float = 2.0          # second target
    scale_out: float = 0.5          # fraction closed at target 1 (0 = all-out at target 2)
    breakeven_after_t1: bool = True
    max_hold_bars: int = 20         # time stop
    flatten_at_session_end: bool = True

    # --- costs and sizing -----------------------------------------------
    tick_size: float = 0.01
    slippage_ticks: float = 1.0     # on every market/stop fill
    commission_per_unit: float = 0.005   # per share/contract, per side
    point_value: float = 1.0        # currency per 1.00 of price per unit
    starting_equity: float = 25_000.0
    risk_per_trade_pct: float = 0.5  # of current equity
    max_leverage: float = 4.0       # notional cap as a multiple of equity

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signal:
    bar: int            # index of the signal bar
    t: int
    side: int           # +1 long, -1 short
    entry: float        # limit price: the POC
    stop: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 8)


def find_signals(bars: list[FootprintBar], p: StrategyParams) -> list[Signal]:
    """
    Scan bars for the setup. Each signal is a function of its own bar and the
    bars before it -- never the bars after.
    """
    out: list[Signal] = []
    vols = [b.volume for b in bars]
    for i, b in enumerate(bars):
        if i < p.volume_lookback:
            continue
        # The median window stops BEFORE bar i and stays inside its session.
        window = [vols[j] for j in range(i - p.volume_lookback, i)
                  if bars[j].session == b.session]
        if len(window) < max(5, p.volume_lookback // 2):
            continue
        med = float(np.median(window))
        if med <= 0 or b.volume < p.volume_mult * med:
            continue
        if (b.h - b.l) < p.min_range_ticks * p.tick_size:
            continue

        side = 0
        if p.allow_long and b.poc_pos <= p.poc_zone and b.c > b.poc:
            side = 1
        elif p.allow_short and b.poc_pos >= 1.0 - p.poc_zone and b.c < b.poc:
            side = -1
        if side == 0:
            continue
        if p.require_delta and b.delta * side <= 0:
            continue
        if p.min_stack > 0 and (b.buy_stack if side > 0 else b.sell_stack) < p.min_stack:
            continue
        if p.trend_filter and (b.c - b.vwap) * side <= 0:
            continue

        out.append(_make_signal(i, b, side, p, "poc_defence"))
    return out


def _make_signal(i: int, b: FootprintBar, side: int, p: StrategyParams,
                 reason: str) -> Signal:
    entry = _round_tick(b.poc, p.tick_size)
    buffer = p.stop_buffer_ticks * p.tick_size
    floor = p.min_risk_ticks * p.tick_size
    if side > 0:
        stop = min(b.l - buffer, entry - floor)
    else:
        stop = max(b.h + buffer, entry + floor)
    return Signal(bar=i, t=b.t, side=side, entry=entry,
                  stop=_round_tick(stop, p.tick_size), reason=reason)


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

@dataclass
class TradeResult:
    signal_bar: int
    side: int
    entry_t: float
    entry: float
    stop: float
    target1: float
    target2: float
    exit_t: float
    exit: float                 # volume-weighted exit price across scale-outs
    qty: float
    pnl: float                  # net of costs, in currency
    r: float                    # net of costs, in multiples of initial risk
    costs: float
    hold_bars: int
    reason: str                 # how the final piece left: target2, stop, breakeven, time, session_end
    hit_t1: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _simulate(bars: list[FootprintBar], tape: TradeTape, signals: list[Signal],
              p: StrategyParams, *, bar_range: tuple[int, int] | None = None,
              ) -> tuple[list[TradeResult], dict]:
    """
    Walk orders through the tape. One order or position at a time: a signal
    that fires while another is pending or open is skipped, which is what a
    single-position account would do.
    """
    lo_bar, hi_bar = bar_range if bar_range else (0, len(bars))
    price, ts, size = tape.price, tape.ts, tape.size
    tick = p.tick_size
    slip = p.slippage_ticks * tick
    eps = tick * 1e-6

    results: list[TradeResult] = []
    equity = p.starting_equity
    stats = {"signals": 0, "filled": 0, "expired": 0, "skipped_busy": 0,
             "skipped_size": 0}
    busy_until_trade = -1                  # tape index up to which we are occupied

    for sig in signals:
        if not (lo_bar <= sig.bar < hi_bar):
            continue
        stats["signals"] += 1
        sb = bars[sig.bar]
        if sb.i1 <= busy_until_trade:
            stats["skipped_busy"] += 1
            continue

        side = sig.side
        risk = abs(sig.entry - sig.stop)
        if risk <= 0:
            continue

        # --- the entry: a resting limit, live from the trade after the signal
        # bar's last, for entry_ttl_bars bars of the same session.
        last_entry_bar = sig.bar
        for j in range(sig.bar + 1, min(sig.bar + 1 + p.entry_ttl_bars, hi_bar)):
            if bars[j].session != sb.session:
                break
            last_entry_bar = j
        if last_entry_bar == sig.bar:
            stats["expired"] += 1
            continue
        a, b = bars[sig.bar + 1].i0, bars[last_entry_bar].i1
        seg = price[a:b]
        if side > 0:
            hit = seg < sig.entry - eps if p.fill_through else seg <= sig.entry + eps
        else:
            hit = seg > sig.entry + eps if p.fill_through else seg >= sig.entry - eps
        where = np.flatnonzero(hit)
        if where.size == 0:
            stats["expired"] += 1
            busy_until_trade = b - 1
            continue
        fill_i = a + int(where[0])

        # --- sizing: fixed fraction of current equity at risk, capped by
        # leverage. Whole units only.
        risk_cash = equity * p.risk_per_trade_pct / 100.0
        qty = math.floor(risk_cash / (risk * p.point_value)) if risk_cash > 0 else 0
        cap = math.floor(equity * p.max_leverage / (sig.entry * p.point_value)) \
            if sig.entry > 0 else 0
        qty = min(qty, cap)
        if qty < 1:
            stats["skipped_size"] += 1
            continue
        stats["filled"] += 1

        entry_px = sig.entry                     # a limit fills at its price
        t1 = _round_tick(entry_px + side * p.target1_r * risk, tick)
        t2 = _round_tick(entry_px + side * p.target2_r * risk, tick)
        stop = sig.stop
        scale = p.scale_out if 0 < p.scale_out < 1 else 0.0
        q1 = math.floor(qty * scale) if scale else 0
        open_qty = qty
        hit_t1 = False
        realised = 0.0                           # gross, in price x qty
        exit_notional = 0.0
        reason = ""

        # Which bar holds the fill? Needed for the time stop.
        entry_bar = sig.bar + 1
        while bars[entry_bar].i1 <= fill_i:
            entry_bar += 1
        last_bar = min(entry_bar + p.max_hold_bars, hi_bar - 1)
        if p.flatten_at_session_end:
            k = entry_bar
            while k + 1 <= last_bar and bars[k + 1].session == sb.session:
                k += 1
            session_capped = k < last_bar
            last_bar = k
        else:
            session_capped = False
        end_i = bars[last_bar].i1                # exclusive

        i = fill_i + 1
        exit_i = fill_i
        while i < end_i and open_qty > 0:
            px_i = price[i]
            # Stop first: when one print could be read as both, assume the worse.
            if (side > 0 and px_i <= stop + eps) or (side < 0 and px_i >= stop - eps):
                # A stop is a market order once touched: filled at the stop or
                # at the print if price gapped through it, then slipped.
                base = min(stop, px_i) if side > 0 else max(stop, px_i)
                fill = base - side * slip
                realised += side * (fill - entry_px) * open_qty
                exit_notional += fill * open_qty
                reason = "breakeven" if hit_t1 and p.breakeven_after_t1 else "stop"
                open_qty = 0
                exit_i = i
                break
            if not hit_t1 and q1 > 0:
                through = (side > 0 and (px_i > t1 + eps if p.fill_through else px_i >= t1 - eps)) or \
                          (side < 0 and (px_i < t1 - eps if p.fill_through else px_i <= t1 + eps))
                if through:
                    realised += side * (t1 - entry_px) * q1
                    exit_notional += t1 * q1
                    open_qty -= q1
                    hit_t1 = True
                    if p.breakeven_after_t1:
                        stop = entry_px
                    exit_i = i
            if open_qty > 0:
                through2 = (side > 0 and (px_i > t2 + eps if p.fill_through else px_i >= t2 - eps)) or \
                           (side < 0 and (px_i < t2 - eps if p.fill_through else px_i <= t2 + eps))
                if through2:
                    realised += side * (t2 - entry_px) * open_qty
                    exit_notional += t2 * open_qty
                    open_qty = 0
                    reason = "target2"
                    hit_t1 = hit_t1 or q1 == 0
                    exit_i = i
                    break
            i += 1

        if open_qty > 0:
            # Time stop or session end: out at the last print in the window.
            exit_i = max(end_i - 1, fill_i)
            fill = price[exit_i] - side * slip
            realised += side * (fill - entry_px) * open_qty
            exit_notional += fill * open_qty
            open_qty = 0
            reason = "session_end" if session_capped else "time"

        # Commission on every unit in and every unit out.
        costs = 2 * qty * p.commission_per_unit
        pnl = realised * p.point_value - costs
        equity += pnl
        exit_bar = entry_bar
        while exit_bar < len(bars) - 1 and bars[exit_bar].i1 <= exit_i:
            exit_bar += 1
        results.append(TradeResult(
            signal_bar=sig.bar, side=side, entry_t=float(ts[fill_i]), entry=entry_px,
            stop=sig.stop, target1=t1, target2=t2, exit_t=float(ts[exit_i]),
            exit=exit_notional / qty, qty=float(qty), pnl=pnl,
            r=pnl / (risk * qty * p.point_value), costs=costs,
            hold_bars=exit_bar - entry_bar, reason=reason, hit_t1=hit_t1))
        busy_until_trade = exit_i

    return results, stats


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval on a win rate. Wide at small n, which is the point."""
    if n == 0:
        return (0.0, 1.0)
    ph = wins / n
    denom = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / denom
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarise(results: list[TradeResult], p: StrategyParams, stats: dict) -> dict:
    n = len(results)
    if n == 0:
        return {"trades": 0, "win_rate": None, "win_rate_ci": None,
                "expectancy_r": None, "avg_win_r": None, "avg_loss_r": None,
                "profit_factor": None, "roi_pct": 0.0, "net_pnl": 0.0,
                "max_drawdown_pct": 0.0, "avg_hold_bars": None, "costs": 0.0,
                "fill_rate": 0.0 if stats.get("signals") else None,
                "t_stat": None, "exits": {}, "by_side": {}, **stats}
    rs = np.array([t.r for t in results])
    pnl = np.array([t.pnl for t in results])
    wins = int((pnl > 0).sum())
    lo, hi = wilson_interval(wins, n)
    gross_win = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())

    eq = p.starting_equity + np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate(([p.starting_equity], eq)))[1:]
    dd = float(((peak - eq) / peak).max())

    exits: dict[str, int] = {}
    for t in results:
        exits[t.reason] = exits.get(t.reason, 0) + 1
    by_side = {}
    for name, s in (("long", 1), ("short", -1)):
        sub = [t for t in results if t.side == s]
        if sub:
            by_side[name] = {
                "trades": len(sub),
                "win_rate": float(sum(t.pnl > 0 for t in sub) / len(sub)),
                "expectancy_r": float(np.mean([t.r for t in sub])),
            }

    # Is the mean R distinguishable from zero? A one-sample t statistic. Under
    # ~2 in absolute value, the honest reading is "cannot tell from noise".
    t_stat = float(rs.mean() / (rs.std(ddof=1) / math.sqrt(n))) if n > 1 and rs.std(ddof=1) > 0 else None

    return {
        "trades": n,
        "win_rate": wins / n,
        "win_rate_ci": [lo, hi],
        "expectancy_r": float(rs.mean()),
        "avg_win_r": float(rs[rs > 0].mean()) if (rs > 0).any() else None,
        "avg_loss_r": float(rs[rs <= 0].mean()) if (rs <= 0).any() else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "roi_pct": float((eq[-1] / p.starting_equity - 1) * 100),
        "net_pnl": float(pnl.sum()),
        "max_drawdown_pct": dd * 100,
        "avg_hold_bars": float(np.mean([t.hold_bars for t in results])),
        "costs": float(sum(t.costs for t in results)),
        "fill_rate": stats["filled"] / stats["signals"] if stats.get("signals") else None,
        "t_stat": t_stat,
        "exits": exits,
        "by_side": by_side,
        **stats,
    }


def random_control(bars: list[FootprintBar], tape: TradeTape, p: StrategyParams,
                   n_signals: int, *, runs: int = 200, seed: int = 7,
                   bar_range: tuple[int, int] | None = None) -> dict | None:
    """
    The same order mechanics on bars and directions chosen at random.

    Each run draws `n_signals` bars uniformly from the eligible ones, flips a
    coin for direction, and places exactly the order the strategy would: a
    limit at that bar's POC, stop beyond its extreme, same targets, same costs.
    What differs is only *which bars* -- so the gap between the strategy and
    this distribution is the value of the signal, with the exit geometry
    subtracted out.
    """
    lo_bar, hi_bar = bar_range if bar_range else (0, len(bars))
    eligible = [i for i in range(max(lo_bar, p.volume_lookback), hi_bar)
                if (bars[i].h - bars[i].l) >= p.min_range_ticks * p.tick_size]
    if n_signals < 1 or len(eligible) < n_signals:
        return None
    rng = random.Random(seed)
    exp, wr = [], []
    for _ in range(runs):
        picks = sorted(rng.sample(eligible, n_signals))
        sigs = [_make_signal(i, bars[i], rng.choice((1, -1)), p, "random") for i in picks]
        res, _ = _simulate(bars, tape, sigs, p, bar_range=bar_range)
        if res:
            exp.append(float(np.mean([t.r for t in res])))
            wr.append(sum(t.pnl > 0 for t in res) / len(res))
    if not exp:
        return None
    return {
        "runs": len(exp),
        "expectancy_r_mean": float(np.mean(exp)),
        "expectancy_r_p05": float(np.percentile(exp, 5)),
        "expectancy_r_p95": float(np.percentile(exp, 95)),
        "win_rate_mean": float(np.mean(wr)),
        "win_rate_p05": float(np.percentile(wr, 5)),
        "win_rate_p95": float(np.percentile(wr, 95)),
        "_exp": exp,
    }


def _verdict(summary: dict, control: dict | None, tape: TradeTape) -> dict:
    """One plain-language reading of the result, most disqualifying fact first."""
    notes: list[str] = []
    n = summary["trades"]
    if tape.classified == "generated":
        level = "synthetic"
        notes.append("Generated tape with no edge built in: this run checks the "
                     "machinery and nothing else. Load real trades to learn anything.")
    elif n < 30:
        level = "insufficient"
        notes.append(f"{n} trades is too few to distinguish skill from luck; the "
                     "win-rate interval shows how wide the uncertainty is.")
    elif summary["expectancy_r"] <= 0:
        level = "negative"
        notes.append("Expectancy is negative after costs.")
    elif control and summary.get("control_percentile") is not None \
            and summary["control_percentile"] < 95:
        level = "no_edge_over_random"
        notes.append("Profitable, but not better than random entries with the same "
                     "exits — the result comes from the exit geometry or the "
                     "sample's drift, not from the signal.")
    elif summary["t_stat"] is None or abs(summary["t_stat"]) < 2:
        level = "inconclusive"
        notes.append("Positive, but the mean is within noise of zero (|t| < 2).")
    else:
        level = "candidate"
        notes.append("Positive expectancy that beats the random control on this "
                     "sample. That is a candidate for out-of-sample testing on "
                     "different days, not a result.")
    if tape.classified == "tick_rule":
        notes.append("Aggressor side was inferred by the tick rule; delta and "
                     "imbalance are estimates.")
    if control and summary["win_rate"] is not None:
        notes.append(f"Random entries with these exits win "
                     f"{control['win_rate_mean'] * 100:.0f}% of the time — that "
                     "much of the win rate is the exit rules, not the entries.")
    return {"level": level, "notes": notes}


def run_backtest(bars: list[FootprintBar], tape: TradeTape, p: StrategyParams, *,
                 control_runs: int = 200, bar_range: tuple[int, int] | None = None,
                 ) -> dict:
    """Signals, simulated trades, summary, random control, and a verdict."""
    signals = find_signals(bars, p)
    results, stats = _simulate(bars, tape, signals, p, bar_range=bar_range)
    summary = summarise(results, p, stats)

    control = None
    if control_runs > 0 and stats["signals"] > 0:
        control = random_control(bars, tape, p, stats["signals"], runs=control_runs,
                                 bar_range=bar_range)
    if control and summary["expectancy_r"] is not None:
        exp = control.pop("_exp")
        summary["control_percentile"] = float(
            100.0 * sum(e < summary["expectancy_r"] for e in exp) / len(exp))
    else:
        summary["control_percentile"] = None
        if control:
            control.pop("_exp", None)

    equity = p.starting_equity
    curve = [{"t": None, "equity": equity}]
    for t in results:
        equity += t.pnl
        curve.append({"t": t.exit_t, "equity": equity})

    lo_bar, hi_bar = bar_range if bar_range else (0, len(bars))
    return {
        "params": p.to_dict(),
        "summary": summary,
        "control": control,
        "verdict": _verdict(summary, control, tape),
        "signals": [s.to_dict() for s in signals if lo_bar <= s.bar < hi_bar],
        "trades": [t.to_dict() for t in results],
        "equity_curve": curve,
    }


# --------------------------------------------------------------------------
# Walk-forward sweep
# --------------------------------------------------------------------------

@dataclass
class SweepGrid:
    volume_mult: list[float] = field(default_factory=lambda: [1.25, 1.5, 2.0])
    poc_zone: list[float] = field(default_factory=lambda: [0.25, 0.35])
    target2_r: list[float] = field(default_factory=lambda: [1.5, 2.0, 3.0])
    scale_out: list[float] = field(default_factory=lambda: [0.0, 0.5])
    entry_ttl_bars: list[int] = field(default_factory=lambda: [2, 4])

    def variants(self) -> list[dict]:
        out = []
        for vm in self.volume_mult:
            for pz in self.poc_zone:
                for t2 in self.target2_r:
                    for so in self.scale_out:
                        for ttl in self.entry_ttl_bars:
                            out.append({"volume_mult": vm, "poc_zone": pz,
                                        "target2_r": t2, "scale_out": so,
                                        "entry_ttl_bars": ttl})
        return out


def run_sweep(bars: list[FootprintBar], tape: TradeTape, base: StrategyParams, *,
              grid: SweepGrid | None = None, train_fraction: float = 0.6,
              min_trades: int = 20) -> dict:
    """
    Pick parameters on the first part of the tape; score them on the rest.

    Trying 72 parameter sets on one tape and reporting the best is how a
    strategy with no edge acquires a 70% win rate. So the best set is chosen
    using only the training bars, and the number that is reported as the result
    is what that set then did on bars the choice never saw. Both are shown; the
    gap between them is the overfitting.

    Variants are ranked by expectancy in R, with a floor on trade count -- a
    variant that traded four times and won four is not the best, it is the
    least tested.
    """
    grid = grid or SweepGrid()
    split = int(len(bars) * train_fraction)
    train, test = (0, split), (split, len(bars))

    rows = []
    for v in grid.variants():
        p = StrategyParams(**{**base.to_dict(), **v})
        if p.target2_r <= p.target1_r and p.scale_out > 0:
            continue
        sigs = find_signals(bars, p)
        r_in, s_in = _simulate(bars, tape, sigs, p, bar_range=train)
        r_out, s_out = _simulate(bars, tape, sigs, p, bar_range=test)
        a, b = summarise(r_in, p, s_in), summarise(r_out, p, s_out)
        rows.append({"variant": v,
                     "train": _slim(a), "test": _slim(b)})

    ranked = [r for r in rows if r["train"]["trades"] >= min_trades]
    ranked.sort(key=lambda r: r["train"]["expectancy_r"], reverse=True)
    best = ranked[0] if ranked else None

    tested = [r["test"]["expectancy_r"] for r in rows
              if r["test"]["trades"] >= max(5, min_trades // 4)]
    selection = None
    if best and best["test"]["expectancy_r"] is not None and tested:
        selection = {
            # Where the chosen variant's out-of-sample result falls among ALL
            # variants' out-of-sample results. 50 means choosing in-sample did
            # no better than picking a variant at random.
            "test_percentile": float(100.0 * sum(e <= best["test"]["expectancy_r"]
                                                 for e in tested) / len(tested)),
            "median_test_expectancy_r": float(np.median(tested)),
            "degradation_r": float(best["train"]["expectancy_r"]
                                   - best["test"]["expectancy_r"]),
        }
    return {
        "variants_tried": len(rows),
        "train_bars": split,
        "test_bars": len(bars) - split,
        "min_trades": min_trades,
        "best": best,
        "selection": selection,
        "table": sorted(rows, key=lambda r: (r["train"]["expectancy_r"]
                                             if r["train"]["expectancy_r"] is not None
                                             else -9e9), reverse=True),
        "synthetic": tape.classified == "generated",
    }


def _slim(s: dict) -> dict:
    return {k: s.get(k) for k in ("trades", "win_rate", "expectancy_r",
                                  "profit_factor", "roi_pct", "max_drawdown_pct")}
