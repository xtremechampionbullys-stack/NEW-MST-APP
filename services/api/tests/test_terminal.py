"""
Terminal panels: key statistics and the series scan.

The scan is the part that needs testing hardest, because every one of its
outputs is the kind of claim a user will act on. A "support level" that is
really one bar's low, or an "uptrend" that is really a flat line with noise, is
worse than no panel at all — it carries the authority of a computed result
without the substance of one.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from app.providers.base import Bar, Quote
from app.services.terminal import key_stats, scan


def make_bars(closes, *, spread=0.01, volume=1_000_000.0):
    """Daily bars from a close series, with highs and lows around each close."""
    start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    return [
        Bar(t=start + dt.timedelta(days=i), o=c, h=c * (1 + spread),
            l=c * (1 - spread), c=c, v=volume)
        for i, c in enumerate(closes)
    ]


def make_quote(last, **kw):
    return Quote(symbol="TEST", last=last, **kw)


# ------------------------------------------------------------- key stats

def test_52_week_range_reports_how_many_sessions_it_actually_used():
    """
    A range over 80 bars is not a 52-week range.

    The label is the part people read, so the payload has to carry enough for
    the UI to label it correctly rather than assuming a full year of history.
    """
    bars = make_bars([100 + i for i in range(80)])
    out = key_stats("TEST", make_quote(180.0), bars, None, None)
    assert out["range_52w"]["sessions"] == 80
    assert out["range_52w"]["complete"] is False

    long_bars = make_bars([100 + i * 0.1 for i in range(300)])
    out2 = key_stats("TEST", make_quote(120.0), long_bars, None, None)
    assert out2["range_52w"]["sessions"] == 252
    assert out2["range_52w"]["complete"] is True


def test_52_week_range_uses_only_the_trailing_window():
    """An old spike outside the window must not leak into the range."""
    closes = [500.0] + [100.0] * 300           # spike is 301 bars back
    out = key_stats("TEST", make_quote(100.0), make_bars(closes), None, None)
    assert out["range_52w"]["high"] < 200.0


def test_average_volume_reports_its_own_sample_size():
    bars = make_bars([100.0] * 9, volume=2_000_000.0)
    out = key_stats("TEST", make_quote(100.0), bars, None, None)
    assert out["avg_volume_sessions"] == 9
    assert out["avg_volume"] == pytest.approx(2_000_000.0)


def test_market_cap_falls_back_to_shares_times_price():
    bars = make_bars([100.0] * 40)
    profile = {"shares_outstanding": 1_000_000, "market_cap": None}
    out = key_stats("TEST", make_quote(50.0), bars, profile, None)
    assert out["market_cap"] == pytest.approx(50_000_000)


def test_projected_earnings_date_is_flagged_as_projected():
    bars = make_bars([100.0] * 40)
    earnings = {"next_event": {"days_away": 25, "is_upcoming": True, "is_confirmed": False}}
    out = key_stats("TEST", make_quote(100.0), bars, None, earnings)
    assert out["days_to_earnings"] == 25
    assert "projected" in out["earnings_note"]


def test_confirmed_earnings_date_carries_no_caveat():
    bars = make_bars([100.0] * 40)
    earnings = {"next_event": {"days_away": 3, "is_upcoming": True, "is_confirmed": True}}
    out = key_stats("TEST", make_quote(100.0), bars, None, earnings)
    assert out["days_to_earnings"] == 3
    assert out["earnings_note"] is None


def test_instrument_that_does_not_report_earnings_says_so():
    """An ETF has no earnings date. That is a fact, not a missing value."""
    bars = make_bars([100.0] * 40)
    out = key_stats("SPY", make_quote(100.0), bars, None, {"reports_earnings": False})
    assert out["days_to_earnings"] is None
    assert "does not report earnings" in out["earnings_note"]


# ------------------------------------------------------------------ scan

def kinds(result) -> set[str]:
    return {f["kind"] for f in result["findings"]}


def label_for(result, kind) -> str | None:
    for f in result["findings"]:
        if f["kind"] == kind:
            return f["label"]
    return None


def test_scan_refuses_to_report_on_too_little_history():
    bars = make_bars([100.0 + i for i in range(20)])
    out = scan("TEST", bars, make_quote(120.0))
    assert out["findings"] == []
    assert "30" in out["note"]


def test_scan_states_it_reads_the_series_not_the_image():
    """
    The whole product claim rests on this. If the note ever stops saying so, the
    feature starts implying a vision model that does not exist.
    """
    bars = make_bars([100.0 + i * 0.5 for i in range(260)])
    out = scan("TEST", bars, make_quote(230.0))
    assert out["reads"] == "price series"
    assert "does not look at the rendered image" in out["note"]


def test_rising_series_reads_as_an_uptrend():
    bars = make_bars([100.0 + i * 0.5 for i in range(260)])
    out = scan("TEST", bars, make_quote(230.0))
    assert label_for(out, "trend") == "uptrend"


def test_falling_series_reads_as_a_downtrend():
    bars = make_bars([300.0 - i * 0.5 for i in range(260)])
    out = scan("TEST", bars, make_quote(170.0))
    assert label_for(out, "trend") == "downtrend"


def test_tangled_averages_report_no_clear_trend_rather_than_guessing():
    """
    When the averages are not stacked, the honest output is "no clear trend" —
    not whichever of up or down is marginally ahead.

    A cycling series is the case that produces this. A simple rise-then-fall
    does *not*: by the end of it the last 130 bars are falling steadily and the
    averages really are stacked downward, so "downtrend" would be the correct
    answer there. That was this test's first premise and it was wrong about the
    code rather than the code being wrong — worth recording, because the
    tempting fix at that moment is to weaken the detector.
    """
    closes = [150.0 + 30 * math.sin(i / 25.0) for i in range(300)]
    out = scan("TEST", make_bars(closes), make_quote(closes[-1]))
    assert label_for(out, "trend") == "no clear trend"


def test_a_sustained_decline_is_called_a_downtrend():
    """The counterpart to the above: stacked downward must not read as unclear."""
    closes = [100.0 + i for i in range(130)] + [230.0 - i for i in range(130)]
    out = scan("TEST", make_bars(closes), make_quote(closes[-1]))
    assert label_for(out, "trend") == "downtrend"


def test_levels_need_more_than_one_touch():
    """
    A single pivot is one bar's high — a fact about that bar, not a level. With
    a strictly monotonic series every pivot is unique, so nothing should be
    reported as support or resistance.
    """
    bars = make_bars([100.0 + i * 0.5 for i in range(260)])
    out = scan("TEST", bars, make_quote(bars[-1].c))
    assert "resistance" not in kinds(out)
    assert "support" not in kinds(out)


def test_a_repeatedly_tested_ceiling_is_reported_as_resistance():
    """An oscillation between two prices should surface both sides."""
    closes = []
    for _ in range(14):
        closes += [100, 104, 108, 112, 116, 120, 116, 112, 108, 104]
    bars = make_bars([float(c) for c in closes])
    out = scan("TEST", bars, make_quote(110.0))

    levels = [f for f in out["findings"] if f["kind"] in ("support", "resistance")]
    assert levels, "an oscillating series must produce levels"
    assert all(f["values"]["touches"] > 1 for f in levels)
    # Resistance sits above spot and support below — the sign convention is the
    # thing a user reads off the panel, so it is worth pinning.
    for f in levels:
        if f["kind"] == "resistance":
            assert f["values"]["price"] > 110.0
        else:
            assert f["values"]["price"] < 110.0


def test_findings_carry_the_numbers_that_produced_them():
    """
    Every finding must be checkable. A label with no evidence behind it cannot
    be caught being wrong, which is the property that makes the panel safe.
    """
    closes = []
    for _ in range(14):
        closes += [100, 104, 108, 112, 116, 120, 116, 112, 108, 104]
    out = scan("TEST", make_bars([float(c) for c in closes]), make_quote(110.0))
    for f in out["findings"]:
        assert f["detail"], f"{f['kind']} has no supporting detail"
        assert isinstance(f["values"], dict) and f["values"], f"{f['kind']} has no values"


def test_quiet_series_reads_as_lower_volatility_than_a_wild_one():
    """
    The regime label is relative to the instrument's own history, so the test
    compares two series rather than asserting an absolute threshold.
    """
    calm = [100.0 * math.exp(0.0005 * i) for i in range(300)]
    wild = [100.0 * math.exp(0.0005 * i + (0.06 if i % 2 else -0.055)) for i in range(300)]

    calm_out = scan("CALM", make_bars(calm), make_quote(calm[-1]))
    wild_out = scan("WILD", make_bars(wild), make_quote(wild[-1]))

    calm_v = next(f for f in calm_out["findings"] if f["kind"] == "volatility")
    wild_v = next(f for f in wild_out["findings"] if f["kind"] == "volatility")
    assert wild_v["values"]["realized_20d"] > calm_v["values"]["realized_20d"] * 3


def test_rsi_matches_a_hand_computed_value_on_a_known_series():
    """
    A monotonically rising series has no down closes, so Wilder's RSI pins at
    100. This is the one RSI input with an unambiguous answer, which makes it
    the one worth asserting exactly.
    """
    bars = make_bars([100.0 + i for i in range(60)])
    out = scan("TEST", bars, make_quote(159.0))
    momentum = next(f for f in out["findings"] if f["kind"] == "momentum")
    assert momentum["values"]["rsi14"] == pytest.approx(100.0)
    assert momentum["label"] == "overbought"
