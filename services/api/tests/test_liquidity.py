"""
Volume + order + liquidity map.

The detectors are tested on hand-built series where the right answer is known
by construction — a zone that must exist, a level that must be major, a pool
that must have been swept. Running them only against the synthetic provider
would prove the code returns *something*, which it always does.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import Bar
from app.services.liquidity import MIN_BARS, liquidity_map


def series(rows: list[tuple[float, float, float, float]], *, volume: float = 1000.0,
           trades: int | None = 10) -> list[Bar]:
    """Bars from (o, h, l, c) rows, one per day."""
    start = dt.datetime(2025, 1, 1, 20, tzinfo=dt.timezone.utc)
    return [Bar(t=start + dt.timedelta(days=i), o=o, h=h, l=l, c=c, v=volume, n=trades)
            for i, (o, h, l, c) in enumerate(rows)]


def flat(n: int, price: float = 100.0) -> list[tuple[float, float, float, float]]:
    """A tight range that produces no structure of its own."""
    return [(price, price + 0.5, price - 0.5, price) for _ in range(n)]


def bullish_block_series() -> list[tuple[float, float, float, float]]:
    """
    Range, a swing high at 104, a down candle at 99-101, then a rally that
    closes through 104. The down candle is the bullish order block.
    """
    rows = flat(30)
    rows += [(100, 102, 99.5, 101.5), (101.5, 104, 101, 103),      # swing high 104
             (103, 103.5, 101.5, 102), (102, 102.5, 100.5, 101),
             (101, 101.5, 100, 100.5), (100.5, 101, 99.8, 100.2)]
    rows += [(101, 101, 99, 99.2)]                                  # the block: 99-101
    rows += [(99.2, 102, 99.1, 101.8), (101.8, 105.5, 101.6, 105.2)]  # closes > 104
    rows += [(105.2, 107, 104.8, 106.5), (106.5, 108, 106, 107.5),
             (107.5, 108.5, 106.5, 107), (107, 107.5, 105.5, 106),
             (106, 106.5, 104.5, 105)]
    return rows


# --------------------------------------------------------------------------
# Order blocks and their counters
# --------------------------------------------------------------------------

def test_too_few_bars_reports_nothing_rather_than_something_weak():
    out = liquidity_map("X", series(flat(MIN_BARS - 1)))
    assert out["order_blocks"] == [] and out["levels"] == [] and out["profile"] is None
    assert str(MIN_BARS) in out["note"]


def test_bullish_order_block_is_the_last_down_candle_before_the_break():
    rows = bullish_block_series()
    out = liquidity_map("X", series(rows))
    bulls = [z for z in out["order_blocks"] if z["side"] == "bullish"]
    assert bulls, "the down candle before the break of 104 must be found"
    z = min(bulls, key=lambda z: abs(z["bottom"] - 99))
    assert (z["bottom"], z["top"]) == (99, 101)
    assert z["status"] == "fresh" and z["touches"] == 0
    assert z["formed_t"] < z["confirmed_t"]


def test_touch_counter_counts_separate_returns_not_bars():
    rows = bullish_block_series()
    # Two separate visits, the first lasting two bars.
    rows += [(105, 105.2, 100.5, 102), (102, 103, 100.2, 102.8),
             (102.8, 106, 102.5, 105.5), (105.5, 106, 104, 105),
             (105, 105.2, 100.8, 103), (103, 106, 102.9, 105.8)]
    out = liquidity_map("X", series(rows))
    z = next(z for z in out["order_blocks"] if z["bottom"] == 99)
    assert z["touches"] == 2
    assert z["status"] == "tested"
    assert z["bars_in_zone"] > z["touches"]


def test_zone_volume_and_orders_are_prorated_by_range_overlap():
    rows = bullish_block_series()
    # One retest bar spanning 100-104: a quarter of its range is in the 99-101 zone.
    rows += [(104, 104, 100, 103.5), (103.5, 106, 103.2, 105.5)]
    bars = series(rows, volume=1000.0, trades=40)
    before = next(z for z in liquidity_map("X", bars[:-2])["order_blocks"]
                  if z["bottom"] == 99)
    after = next(z for z in liquidity_map("X", bars)["order_blocks"]
                 if z["bottom"] == 99)
    assert after["volume"] - before["volume"] == 250
    assert after["orders"] - before["orders"] == 10
    assert after["buy_volume"] + after["sell_volume"] == pytest.approx(after["volume"], abs=1)
    assert after["delta"] == after["buy_volume"] - after["sell_volume"]


def test_a_close_through_the_zone_breaks_it_but_a_wick_does_not():
    rows = bullish_block_series()
    wick = rows + [(105, 105, 98.5, 100.5), (100.5, 104, 100.4, 103.5)]
    out = liquidity_map("X", series(wick))
    assert any(z["bottom"] == 99 and z["status"] == "tested" for z in out["order_blocks"])

    closed = rows + [(105, 105, 98, 98.4), (98.4, 99, 97.5, 98)]
    out = liquidity_map("X", series(closed))
    assert not any(z["bottom"] == 99 for z in out["order_blocks"]), \
        "a broken zone must not be drawn as live"
    assert out["summary"]["zones_broken"] >= 1


def test_orders_are_null_when_the_provider_reports_no_trade_count():
    out = liquidity_map("X", series(bullish_block_series(), trades=None))
    assert out["order_count_source"] is None
    assert out["order_blocks"]
    assert all(z["orders"] is None for z in out["order_blocks"])
    assert all(b["orders"] is None for b in out["profile"]["bins"])


# --------------------------------------------------------------------------
# Support / resistance and liquidity pools
# --------------------------------------------------------------------------

def bounce(low: float, high: float, cycles: int) -> list[tuple[float, float, float, float]]:
    """Price oscillating between a floor and a ceiling, one swing each way per cycle."""
    rows = []
    for _ in range(cycles):
        rows += [(low + 2, low + 2.5, low, low + 1), (low + 1, low + 4, low + 0.8, low + 3.5),
                 (low + 3.5, high - 3, low + 3, high - 3.5), (high - 3.5, high - 1, high - 4, high - 1.5),
                 (high - 1.5, high, high - 2.5, high - 2), (high - 2, high - 1.8, high - 4.5, high - 4),
                 (high - 4, high - 3.5, low + 4, low + 4.5), (low + 4.5, low + 5, low + 1.5, low + 2)]
    return rows


def test_repeatedly_defended_levels_are_major_support_and_resistance():
    rows = bounce(90, 110, 6) + [(92, 101, 91.5, 100)]
    out = liquidity_map("X", series(rows))
    sup = [lv for lv in out["levels"] if lv["kind"] == "support"]
    res = [lv for lv in out["levels"] if lv["kind"] == "resistance"]
    assert any(lv["major"] and abs(lv["price"] - 90) < 1 and lv["touches"] >= 3 for lv in sup)
    assert any(lv["major"] and abs(lv["price"] - 110) < 1 and lv["touches"] >= 3 for lv in res)
    assert out["summary"]["major_support"] == pytest.approx(90, abs=1)
    assert out["summary"]["major_resistance"] == pytest.approx(110, abs=1)
    assert all(lv["price"] < out["spot"] for lv in sup)
    assert all(lv["price"] > out["spot"] for lv in res)


def test_major_stays_rare():
    out = liquidity_map("X", series(bounce(90, 110, 6) + [(92, 101, 91.5, 100)]))
    for kind in ("support", "resistance"):
        assert sum(1 for lv in out["levels"] if lv["kind"] == kind and lv["major"]) <= 2


def test_levels_do_not_chain_into_one_band():
    """A staircase of pivots must not merge into a single many-touch level."""
    rows = []
    for step in range(12):
        base = 100 + step * 1.2
        rows += [(base, base + 1, base - 1, base + 0.5), (base + 0.5, base + 3, base, base + 2.5),
                 (base + 2.5, base + 2.8, base + 0.4, base + 0.8), (base + 0.8, base + 1.5, base - 0.2, base + 1)]
    out = liquidity_map("X", series(rows))
    assert out["levels"]
    assert max(lv["touches"] for lv in out["levels"]) < 12


def test_equal_highs_are_buy_side_liquidity_until_swept():
    rows = bounce(90, 110, 6) + [(92, 101, 91.5, 100)]
    out = liquidity_map("X", series(rows))
    buy = [p for p in out["liquidity"] if p["side"] == "buy"]
    sell = [p for p in out["liquidity"] if p["side"] == "sell"]
    assert any(p["price"] == 110 and p["touches"] >= 2 for p in buy)
    assert any(p["price"] == 90 and p["touches"] >= 2 for p in sell)

    swept = rows + [(100, 111, 99.5, 108), (108, 109, 104, 105), (105, 106, 103, 104),
                    (104, 105, 102, 103), (103, 104, 101, 102)]
    out = liquidity_map("X", series(swept))
    assert not any(p["side"] == "buy" and p["price"] == 110 for p in out["liquidity"])


# --------------------------------------------------------------------------
# Volume profile
# --------------------------------------------------------------------------

def test_profile_conserves_volume_and_brackets_the_point_of_control():
    bars = series(bounce(90, 110, 6), volume=500.0)
    p = liquidity_map("X", bars)["profile"]
    assert sum(b["volume"] for b in p["bins"]) == pytest.approx(500.0 * len(bars), rel=1e-3)
    assert p["value_area_low"] <= p["poc"] <= p["value_area_high"]
    inside = sum(b["volume"] for b in p["bins"]
                 if b["lo"] >= p["value_area_low"] - 1e-6 and b["hi"] <= p["value_area_high"] + 1e-6)
    assert inside >= 0.70 * p["total_volume"] - 1


# --------------------------------------------------------------------------
# No lookahead
# --------------------------------------------------------------------------

def test_zones_never_use_bars_that_did_not_exist_yet():
    """
    A zone confirmed at bar i must be found from bars[:i+1] alone. If trimming
    the future changes where a past zone sits, the detector is reading ahead.
    """
    from app.providers.synthetic import SyntheticProvider
    bars = SyntheticProvider()._series("NVDA", days=300)
    full = liquidity_map("NVDA", bars, max_zones=12)
    assert full["order_blocks"]
    by_t = {int(b.t.timestamp()): i for i, b in enumerate(bars)}
    for z in full["order_blocks"]:
        cut = by_t[z["confirmed_t"]] + 1
        past = liquidity_map("NVDA", bars[:cut], max_zones=12)
        assert any(p["formed_t"] == z["formed_t"] and p["bottom"] == z["bottom"]
                   and p["top"] == z["top"] for p in past["order_blocks"]), \
            f"{z['id']} is not visible on the bar that confirmed it"


# --------------------------------------------------------------------------
# Endpoint contract — the keys the web app binds to
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_endpoint_shape(client):
    r = client.get("/api/market/liquidity/AAPL")
    assert r.status_code == 200
    d = r.json()
    for key in ("symbol", "spot", "atr", "from_t", "to_t", "synthetic",
                "order_count_source", "order_blocks", "liquidity", "levels",
                "profile", "summary", "method", "limits", "note"):
        assert key in d, f"missing {key}"
    assert d["symbol"] == "AAPL"
    for z in d["order_blocks"]:
        for key in ("id", "side", "bottom", "top", "mid", "formed_t", "confirmed_t",
                    "status", "touches", "bars_in_zone", "volume", "buy_volume",
                    "sell_volume", "delta", "orders", "formation_rel_volume",
                    "distance_pct"):
            assert key in z, f"zone missing {key}"
        assert z["bottom"] < z["top"]
        assert z["side"] in ("bullish", "bearish")
        assert z["status"] in ("fresh", "tested")
    for lv in d["levels"]:
        assert lv["kind"] in ("support", "resistance")
        assert isinstance(lv["major"], bool) and 0 <= lv["strength"] <= 100
    assert "order book" in d["limits"]


def test_endpoint_respects_max_zones_and_says_when_data_is_generated(client):
    d = client.get("/api/market/liquidity/SPY", params={"max_zones": 2}).json()
    assert len(d["order_blocks"]) <= 2
    if d["synthetic"]:
        assert d["order_count_source"] == "generated"
        assert "synthetic" in d["note"]


def test_synthetic_trade_counts_did_not_move_a_single_price():
    """Adding `n` to synthetic bars must not have shifted the price series."""
    from app.providers.synthetic import SyntheticProvider
    bars = SyntheticProvider()._series("AAPL", days=50)
    assert all(b.n and b.n > 0 for b in bars)
    assert bars[-1].c == 232.0     # the anchor close, unchanged
