"""
Footprint tests.

A footprint backtest can be wrong in ways that all flatter the result: a bar's
own trades filling its own signal, a limit order filling on a touch, a target
counted before the stop that actually came first. None of those raise, and each
turns a losing strategy into a winning one. So beyond checking the arithmetic
of a footprint bar, these tests pin the properties that keep the backtest
honest -- and the last group checks the strongest one available: on a tape
built to have no edge, the engine must not find one.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.engine.footprint import (
    FootprintBar,
    Signal,
    StrategyParams,
    TradeTape,
    _simulate,
    build_footprint,
    find_signals,
    parse_trades_csv,
    run_backtest,
    run_sweep,
    suggest_row_size,
    synthetic_tape,
    tick_rule,
    wilson_interval,
)
from app.main import app


def tape_of(rows, t0=600.0, classified="exchange") -> TradeTape:
    """rows: (seconds_offset, price, size, side)."""
    a = np.array(rows, dtype=np.float64)
    return TradeTape(ts=t0 + a[:, 0], price=a[:, 1], size=a[:, 2],
                     side=a[:, 3].astype(np.int8), classified=classified, source="test")


# --------------------------------------------------------------------------
# Bar arithmetic
# --------------------------------------------------------------------------

def test_rows_poc_delta_and_value_area_on_a_hand_built_bar():
    tape = tape_of([
        (0, 10.02, 10, +1),
        (1, 10.01, 40, -1),
        (2, 10.01, 60, +1),      # 10.01 trades 100: the POC
        (3, 10.00, 30, -1),
        (4, 10.03, 5, +1),
        (5, 10.02, 20, -1),
    ])
    (bar,) = build_footprint(tape, row_size=0.01)
    assert (bar.o, bar.h, bar.l, bar.c) == (10.02, 10.03, 10.00, 10.02)
    assert bar.volume == 165
    assert bar.delta == (10 + 60 + 5) - (40 + 30 + 20)
    # rows run high -> low as [price, bid, ask, flag]
    assert [r[:3] for r in bar.rows] == [
        [10.03, 0, 5], [10.02, 20, 10], [10.01, 40, 60], [10.00, 30, 0]]
    assert bar.poc_volume == 100
    assert 10.01 <= bar.poc <= 10.02          # inside the 10.01 row
    # 70% of 165 = 115.5: POC (100) plus its larger neighbour, 10.02 (30) -> 130
    assert bar.val == pytest.approx(10.01)
    assert bar.vah == pytest.approx(10.03)


def test_bars_split_on_the_minute_and_empty_minutes_are_not_invented():
    tape = tape_of([(0, 10, 1, 1), (59.9, 10, 1, 1), (60, 10, 1, 1), (200, 10, 1, 1)])
    bars = build_footprint(tape, row_size=0.01)
    assert [b.t for b in bars] == [600, 660, 780]       # no bar for 720
    assert [b.volume for b in bars] == [2, 1, 1]


def test_diagonal_imbalance_compares_ask_with_the_bid_one_row_below():
    tape = tape_of([
        (0, 10.00, 10, -1),      # bid volume at 10.00
        (1, 10.01, 90, +1),      # ask volume at 10.01: 9x the bid below it
        (2, 10.01, 50, -1),
        (3, 10.02, 60, +1),
    ])
    (bar,) = build_footprint(tape, row_size=0.01, imbalance_ratio=3.0)
    flags = {r[0]: r[3] for r in bar.rows}
    assert flags[10.01] == 1
    assert bar.buy_imbalances >= 1


def test_cumulative_delta_and_vwap_reset_across_a_session_gap():
    day = 86400
    tape = tape_of([(0, 10, 100, 1), (30, 10, 100, 1),
                    (day, 20, 50, -1), (day + 30, 20, 50, -1)])
    a, b = build_footprint(tape, row_size=0.01)
    assert a.session != b.session
    assert a.cum_delta == 200 and b.cum_delta == -100
    assert b.vwap == pytest.approx(20.0)


def test_tick_rule_carries_direction_through_unchanged_prices():
    side = tick_rule(np.array([10.0, 10.1, 10.1, 10.0, 10.0, 10.2]))
    assert side.tolist()[1:] == [1, 1, -1, -1, 1]
    assert set(tick_rule(np.array([5.0, 5.0, 5.0, 5.0])).tolist()) == {1, -1}


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

def test_csv_with_header_and_side_is_read_as_exchange_classified():
    t = parse_trades_csv("timestamp,price,qty,side\n1700000000000,100.5,3,buy\n"
                         "1700000001000,100.25,2,sell\n")
    assert t.classified == "exchange"
    assert t.ts.tolist() == [1700000000.0, 1700000001.0]     # ms detected
    assert t.side.tolist() == [1, -1]


def test_headerless_csv_is_read_as_binance_aggtrades():
    # id, price, qty, first, last, time(ms), is_buyer_maker, best_match
    t = parse_trades_csv("1,100.0,0.5,1,1,1700000000000,true,true\n"
                         "2,100.1,0.7,2,2,1700000000500,false,true\n")
    # buyer was maker => seller was the aggressor
    assert t.side.tolist() == [-1, 1]


def test_csv_without_side_falls_back_to_the_tick_rule_and_says_so():
    t = parse_trades_csv("time,price,size\n2024-01-02T14:30:00Z,10,1\n"
                         "2024-01-02T14:30:01Z,10.1,1\n2024-01-02T14:30:02Z,10.0,1\n")
    assert t.classified == "tick_rule"
    assert t.side.tolist()[1:] == [1, -1]


def test_csv_missing_a_required_column_is_rejected_by_name():
    with pytest.raises(ValueError, match="size"):
        parse_trades_csv("time,price\n1,2\n3,4\n")


# --------------------------------------------------------------------------
# Order mechanics. These build bars and a tape by hand so the expected fills
# can be worked out on paper.
# --------------------------------------------------------------------------

def _scripted(prices, *, per_bar=4):
    """A tape of 1-lot prints, `per_bar` per minute, and matching bare bars."""
    rows = [(i // per_bar * 60 + (i % per_bar) * 10, p, 1, 1) for i, p in enumerate(prices)]
    tape = tape_of(rows, t0=0.0)
    bars = build_footprint(tape, row_size=0.01)
    return tape, bars


P = StrategyParams(tick_size=0.01, slippage_ticks=0, commission_per_unit=0,
                   target1_r=1.0, target2_r=2.0, scale_out=0.5,
                   starting_equity=100_000, risk_per_trade_pct=1.0,
                   max_leverage=100, entry_ttl_bars=2, max_hold_bars=5)


def _long(entry=10.00, stop=9.90):
    return [Signal(bar=0, t=0, side=1, entry=entry, stop=stop, reason="test")]


def test_a_limit_that_is_only_touched_does_not_fill():
    tape, bars = _scripted([10.05] * 4 + [10.02, 10.00, 10.00, 10.03] + [10.30] * 4)
    res, stats = _simulate(bars, tape, _long(), P)
    assert res == [] and stats["expired"] == 1
    # ...and does fill once touch-fills are allowed, which is the optimistic model
    from dataclasses import replace
    res, _ = _simulate(bars, tape, _long(), replace(P, fill_through=False))
    assert len(res) == 1


def test_the_signal_bars_own_trades_can_never_fill_its_order():
    # The signal bar itself trades far through the entry; nothing after does.
    tape, bars = _scripted([10.05, 9.95, 9.95, 10.05] + [10.05] * 8)
    res, stats = _simulate(bars, tape, _long(), P)
    assert res == [] and stats["expired"] == 1


def test_scale_out_then_breakeven_stop_is_worth_half_an_r():
    # fill at 10.00 (print 9.99), T1 = 10.10 hit (print 10.11), back to entry.
    tape, bars = _scripted([10.05] * 4 + [9.99, 10.05, 10.11, 10.05] + [10.00, 10.0, 10.0, 10.0])
    (t,), _ = _simulate(bars, tape, _long(), P)
    assert t.hit_t1 and t.reason == "breakeven"
    assert t.r == pytest.approx(0.5)             # half the position x 1R, rest flat
    assert t.qty == 10_000                       # 1% of 100k over 0.10 risk


def test_full_target_run_is_one_and_a_half_r_with_half_off_at_one():
    tape, bars = _scripted([10.05] * 4 + [9.99, 10.11, 10.15, 10.21] + [10.2] * 4)
    (t,), _ = _simulate(bars, tape, _long(), P)
    assert t.reason == "target2"
    assert t.r == pytest.approx(0.5 * 1.0 + 0.5 * 2.0)


def test_stop_that_prints_before_the_target_wins_even_inside_one_bar():
    # Same bar goes to the stop first and the target after. An OHLC backtest
    # sees high >= target and low <= stop and has to guess; the tape does not.
    tape, bars = _scripted([10.05] * 4 + [9.99, 9.89, 10.25, 10.25] + [10.25] * 4)
    (t,), _ = _simulate(bars, tape, _long(), P)
    assert t.reason == "stop" and t.r == pytest.approx(-1.1)   # gapped 1 tick past


def test_costs_and_slippage_come_out_of_the_result():
    from dataclasses import replace
    tape, bars = _scripted([10.05] * 4 + [9.99, 9.90, 9.90, 9.90] + [9.9] * 4)
    (free,), _ = _simulate(bars, tape, _long(), P)
    (paid,), _ = _simulate(bars, tape, _long(),
                           replace(P, slippage_ticks=2, commission_per_unit=0.01))
    assert free.r == pytest.approx(-1.0)
    # 2 ticks slippage on the stop + 1c in + 1c out, over a 10c risk
    assert paid.r == pytest.approx(-1.0 - 0.2 - 0.2)
    assert paid.costs == pytest.approx(2 * paid.qty * 0.01)


def test_time_stop_exits_at_the_last_print_of_the_window():
    tape, bars = _scripted([10.05] * 4 + [9.99, 10.02, 10.02, 10.02] + [10.04] * 40)
    (t,), _ = _simulate(bars, tape, _long(), P)
    assert t.reason == "time" and t.exit == pytest.approx(10.04)


def test_only_one_position_at_a_time():
    tape, bars = _scripted([10.05] * 4 + [9.99] + [10.02] * 43)
    sigs = _long() + [Signal(bar=1, t=60, side=1, entry=10.03, stop=9.9, reason="test")]
    res, stats = _simulate(bars, tape, sigs, P)
    assert len(res) == 1 and stats["skipped_busy"] == 1


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

def _bar(i, *, volume=100.0, poc_pos=0.5, c=10.05, delta=0.0, l=10.0, h=10.10):
    return FootprintBar(t=i * 60, o=10.05, h=h, l=l, c=c, volume=volume, delta=delta,
                        cum_delta=0, poc=l + poc_pos * (h - l), poc_volume=volume / 3,
                        poc_pos=poc_pos, vah=h, val=l, vwap=10.05, buy_imbalances=0,
                        sell_imbalances=0, buy_stack=0, sell_stack=0, rows=[], session=0)


def test_long_signal_needs_high_volume_low_poc_close_above_and_buying_delta():
    quiet = [_bar(i) for i in range(20)]
    good = _bar(20, volume=300, poc_pos=0.1, c=10.09, delta=50)
    (sig,) = find_signals(quiet + [good], StrategyParams())
    assert sig.side == 1 and sig.bar == 20
    assert sig.entry == pytest.approx(10.01)            # the POC
    assert sig.stop < good.l                            # beyond the bar's low

    for spoiled in (_bar(20, volume=120, poc_pos=0.1, c=10.09, delta=50),   # not a volume bar
                    _bar(20, volume=300, poc_pos=0.5, c=10.09, delta=50),   # POC mid-bar
                    _bar(20, volume=300, poc_pos=0.1, c=10.005, delta=50),  # closed below POC
                    _bar(20, volume=300, poc_pos=0.1, c=10.09, delta=-50)): # sellers in control
        assert find_signals(quiet + [spoiled], StrategyParams()) == []


def test_short_signal_is_the_mirror_image():
    quiet = [_bar(i) for i in range(20)]
    (sig,) = find_signals(quiet + [_bar(20, volume=300, poc_pos=0.9, c=10.01, delta=-50)],
                          StrategyParams())
    assert sig.side == -1 and sig.stop > 10.10


def test_signals_do_not_change_when_later_data_arrives():
    """No lookahead: what fired by bar N is identical with or without bars after N."""
    tape = synthetic_tape(11, sessions=2)
    bars = build_footprint(tape, row_size=0.01)
    p = StrategyParams()
    cut = len(bars) // 2
    early = TradeTape(ts=tape.ts[:bars[cut].i0], price=tape.price[:bars[cut].i0],
                      size=tape.size[:bars[cut].i0], side=tape.side[:bars[cut].i0])
    a = [s.to_dict() for s in find_signals(build_footprint(early, row_size=0.01), p)]
    b = [s.to_dict() for s in find_signals(bars, p) if s.bar < cut]
    assert a == b and len(a) > 3


# --------------------------------------------------------------------------
# The honesty checks
# --------------------------------------------------------------------------

def test_no_edge_is_found_on_a_tape_built_without_one():
    """
    The synthetic mid is a random walk independent of order flow, so true
    expectancy before costs is zero. Pooled over several seeds with costs off
    and the neutral (touch) fill model, the mean R must be indistinguishable
    from zero. If this fails, the simulator is leaking the future.
    """
    rs = []
    for seed in range(6):
        tape = synthetic_tape(seed, sessions=8)
        bars = build_footprint(tape, row_size=suggest_row_size(tape, 0.01))
        p = StrategyParams(slippage_ticks=0, commission_per_unit=0, fill_through=False)
        res, _ = _simulate(bars, tape, find_signals(bars, p), p)
        rs += [t.r for t in res]
    rs = np.array(rs)
    assert len(rs) > 400
    stderr = rs.std(ddof=1) / np.sqrt(len(rs))
    assert abs(rs.mean()) < 3 * stderr


def test_default_run_on_synthetic_loses_after_costs_and_is_labelled():
    tape = synthetic_tape(3, sessions=6)
    bars = build_footprint(tape, row_size=suggest_row_size(tape, 0.01))
    out = run_backtest(bars, tape, StrategyParams(), control_runs=40)
    assert out["summary"]["expectancy_r"] < 0
    assert out["verdict"]["level"] == "synthetic"
    assert out["control"]["runs"] > 0
    assert 0 <= out["summary"]["control_percentile"] <= 100


def test_scaling_out_raises_win_rate_without_adding_expectancy():
    """The reason a win rate is never reported here without the control."""
    wr, ex = {}, {}
    for scale in (0.0, 0.5):
        wins = n = 0
        rs = []
        for seed in range(5):
            tape = synthetic_tape(seed, sessions=8)
            bars = build_footprint(tape, row_size=suggest_row_size(tape, 0.01))
            p = StrategyParams(scale_out=scale, slippage_ticks=0, commission_per_unit=0,
                               fill_through=False)
            res, _ = _simulate(bars, tape, find_signals(bars, p), p)
            wins += sum(t.pnl > 0 for t in res)
            n += len(res)
            rs += [t.r for t in res]
        wr[scale], ex[scale] = wins / n, float(np.mean(rs))
    assert wr[0.5] > wr[0.0] + 0.08
    assert abs(ex[0.5] - ex[0.0]) < 0.15


def test_synthetic_tape_does_not_depend_on_how_many_sessions_are_requested():
    a, b = synthetic_tape(5, sessions=2), synthetic_tape(5, sessions=5)
    n = len(a)
    assert np.array_equal(a.ts, b.ts[-n:]) and np.array_equal(a.price, b.price[-n:])


def test_sweep_scores_the_chosen_variant_on_bars_it_was_not_chosen_on():
    tape = synthetic_tape(2, sessions=6)
    bars = build_footprint(tape, row_size=suggest_row_size(tape, 0.01))
    out = run_sweep(bars, tape, StrategyParams(), min_trades=10)
    assert out["train_bars"] + out["test_bars"] == len(bars)
    assert out["variants_tried"] > 20 and out["best"] is not None
    assert out["synthetic"] is True
    # every simulated trade in the test column came from a test-window signal
    best = StrategyParams(**{**StrategyParams().to_dict(), **out["best"]["variant"]})
    res, _ = _simulate(bars, tape, find_signals(bars, best), best,
                       bar_range=(out["train_bars"], len(bars)))
    assert all(t.signal_bar >= out["train_bars"] for t in res)
    assert len(res) == out["best"]["test"]["trades"]


def test_wilson_interval_is_wide_on_small_samples():
    lo, hi = wilson_interval(7, 10)
    assert lo < 0.45 and hi > 0.85
    lo, hi = wilson_interval(700, 1000)
    assert hi - lo < 0.06


# --------------------------------------------------------------------------
# Provider and API
# --------------------------------------------------------------------------

def test_massive_trades_paginate_and_are_tick_rule_classified():
    from app.providers.massive import MassiveProvider
    calls = []

    async def fake_get(path, params=None, **_):
        calls.append(path)
        if path.startswith("/v3/trades"):
            return {"results": [{"sip_timestamp": 1_700_000_000_000_000_000, "price": 10.0, "size": 5},
                                {"sip_timestamp": 1_700_000_001_000_000_000, "price": 10.1, "size": 5}],
                    "next_url": "https://api.massive.com/v3/trades/X?cursor=abc"}
        return {"results": [{"sip_timestamp": 1_700_000_002_000_000_000, "price": 10.0, "size": 7}]}

    prov = MassiveProvider("k")
    prov._get = fake_get
    tape = asyncio.run(prov.get_trades("X", sessions=1))
    assert len(calls) == 2 and "cursor" in calls[1]
    assert tape.classified == "tick_rule"
    assert tape.side.tolist()[1:] == [1, -1]
    assert tape.ts[0] == pytest.approx(1_700_000_000.0)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_run_endpoint_shape_and_synthetic_label(client):
    r = client.post("/api/footprint/run", json={"symbol": "SPY", "sessions": 2,
                                                "control_runs": 20, "window": 12})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["meta"]["synthetic"] is True and "SYNTHETIC" in j["meta"]["note"]
    assert j["verdict"]["level"] == "synthetic"
    assert len(j["window"]["bars"]) == 12 and j["window"]["end"] == j["meta"]["bars"]
    row = j["window"]["bars"][0]["rows"][0]
    assert len(row) == 4
    for key in ("trades", "win_rate", "win_rate_ci", "expectancy_r", "profit_factor",
                "roi_pct", "max_drawdown_pct", "fill_rate", "control_percentile", "exits"):
        assert key in j["summary"]
    assert "rows" not in j["bars"][0] and len(j["bars"]) == j["meta"]["bars"]
    if j["trades"]:
        assert {"entry", "stop", "target1", "target2", "exit", "r", "reason",
                "signal_bar"} <= set(j["trades"][0])


def test_upload_then_run_uses_the_real_tape_and_drops_the_synthetic_label(client):
    rng = np.random.default_rng(1)
    px = 100 + np.cumsum(rng.choice([-0.25, 0, 0.25], 30_000))
    lines = ["time,price,size,side"] + [
        f"{1_700_000_000 + i * 0.4:.1f},{p:.2f},{1 + i % 4},{'buy' if rng.random() < .5 else 'sell'}"
        for i, p in enumerate(px)]
    up = client.post("/api/footprint/upload", json={"csv": "\n".join(lines)})
    assert up.status_code == 200, up.text
    assert up.json()["tick_size"] == 0.25 and up.json()["aggressor"] == "exchange"
    r = client.post("/api/footprint/run", json={"tape_id": up.json()["tape_id"],
                                                "control_runs": 10})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["synthetic"] is False
    assert r.json()["verdict"]["level"] != "synthetic"


def test_bad_inputs_are_rejected_with_a_reason(client):
    assert client.post("/api/footprint/upload", json={"csv": "time,price\n1,2\n3,4"}).status_code == 422
    assert client.post("/api/footprint/run", json={"tape_id": "nope"}).status_code == 404
    r = client.post("/api/footprint/run", json={"params": {"target1_r": 2, "target2_r": 1}})
    assert r.status_code == 422


def test_sweep_endpoint(client):
    r = client.post("/api/footprint/sweep", json={"symbol": "SPY", "sessions": 3, "min_trades": 8})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["synthetic"] is True and j["variants_tried"] > 20
    assert {"variant", "train", "test"} <= set(j["table"][0])
