"""
Backtest engine tests.

A backtester is the easiest place in a trading system to produce numbers that
look authoritative and are quietly wrong, so these check invariants and
accounting identities rather than remembered outputs: P/L must reconcile to the
trade ledger, costs must always reduce it, exits must fire when the rules say,
and a lookahead must be impossible by construction.
"""

import datetime as dt
import math

import pytest

from app.engine.backtest import (
    BacktestRules,
    ModelledQuoteSource,
    StoredQuoteSource,
    compute_metrics,
    pl_distribution,
    run_backtest,
)
from app.providers.base import Bar


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def make_bars(n: int = 500, start_price: float = 100.0, *,
              drift: float = 0.0002, vol: float = 0.02,
              seed: int = 3) -> list[Bar]:
    """Deterministic daily bars on business days."""
    import random

    rng = random.Random(seed)
    price = start_price
    bars: list[Bar] = []
    day = dt.date(2023, 1, 2)
    while len(bars) < n:
        if day.weekday() < 5:
            price *= math.exp(rng.gauss(drift, vol))
            bars.append(Bar(
                t=dt.datetime.combine(day, dt.time(20, 0), dt.timezone.utc),
                o=price, h=price * 1.01, l=price * 0.99, c=price, v=1e6))
        day += dt.timedelta(days=1)
    return bars


@pytest.fixture
def bars():
    return make_bars(500)


# --------------------------------------------------------------------------
# Accounting identities
# --------------------------------------------------------------------------

def test_total_pl_reconciles_to_the_trade_ledger(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    ledger_sum = sum(t["pl"] for t in result["trades"])
    assert result["metrics"]["total_pl"] == pytest.approx(ledger_sum, abs=0.01)


def test_win_and_loss_counts_partition_the_trades(bars):
    result = run_backtest(bars, BacktestRules(template_id="iron_condor"))
    m = result["metrics"]
    assert m["wins"] + m["losses"] == m["trades"]
    assert m["win_rate"] == pytest.approx(m["wins"] / m["trades"])


def test_profit_factor_matches_its_definition(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    pls = [t["pl"] for t in result["trades"]]
    gross_win = sum(p for p in pls if p > 0)
    gross_loss = abs(sum(p for p in pls if p <= 0))
    if gross_loss > 1e-9:
        assert result["metrics"]["profit_factor"] == pytest.approx(
            gross_win / gross_loss, rel=1e-3)


def test_profit_factor_is_none_not_infinite_when_nothing_lost():
    """An undefined ratio must not be reported as a number."""
    from app.engine.backtest import Trade

    trades = [
        Trade(entry_date=dt.date(2024, 1, 1), exit_date=dt.date(2024, 1, 15),
              entry_spot=100, exit_spot=101, entry_basis=-100, pl=50,
              exit_reason="profit target", days_held=14, dte_at_entry=45,
              dte_at_exit=31, legs=[], commission=1.3, max_adverse=-10,
              max_favorable=50),
    ]
    equity = [{"equity": 0, "open_positions": 0},
              {"equity": 50, "open_positions": 0}]
    m = compute_metrics(trades, equity)
    assert m["profit_factor"] is None
    assert m["total_pl"] == 50


def test_drawdown_is_never_positive_and_matches_the_curve(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    curve = [row["equity"] for row in result["equity_curve"]]

    peak = 0.0
    worst = 0.0
    for eq in curve:
        peak = max(peak, eq)
        worst = min(worst, eq - peak)

    assert result["metrics"]["max_drawdown"] <= 0
    assert result["metrics"]["max_drawdown"] == pytest.approx(worst, abs=0.01)


# --------------------------------------------------------------------------
# Costs
# --------------------------------------------------------------------------

def test_commissions_always_reduce_pl(bars):
    free = run_backtest(bars, BacktestRules(
        template_id="short_strangle", commission_per_contract=0.0,
        slippage_pct=0.0))
    charged = run_backtest(bars, BacktestRules(
        template_id="short_strangle", commission_per_contract=1.0,
        slippage_pct=0.0))
    assert charged["metrics"]["total_pl"] < free["metrics"]["total_pl"]


def test_slippage_always_works_against_you(bars):
    """
    Slippage must hurt whether the structure is a credit or a debit. A sign
    error here would show up as free money on one side.
    """
    for template in ("short_strangle", "long_call"):
        clean = run_backtest(bars, BacktestRules(
            template_id=template, slippage_pct=0.0, commission_per_contract=0.0))
        slipped = run_backtest(bars, BacktestRules(
            template_id=template, slippage_pct=0.03, commission_per_contract=0.0))
        assert slipped["metrics"]["total_pl"] < clean["metrics"]["total_pl"], (
            f"slippage did not reduce P/L for {template}")


def test_every_trade_records_its_commission(bars):
    result = run_backtest(bars, BacktestRules(
        template_id="iron_condor", commission_per_contract=0.65))
    assert result["trades"]
    for t in result["trades"]:
        # Four legs, entry and exit: 8 x 0.65
        assert t["commission"] == pytest.approx(8 * 0.65, abs=1e-6)


# --------------------------------------------------------------------------
# Exit rules
# --------------------------------------------------------------------------

def test_exit_reasons_are_all_accounted_for(bars):
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", profit_target_pct=0.5, stop_loss_pct=2.0,
        close_at_dte=21))
    reasons = {t["exit_reason"] for t in result["trades"]}
    assert reasons
    for r in reasons:
        assert (r.startswith("profit target") or r.startswith("stop loss")
                or r.endswith("DTE") or r in ("expired", "end of backtest"))


def test_profit_target_trades_actually_hit_the_target(bars):
    target = 0.5
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", profit_target_pct=target,
        stop_loss_pct=None, close_at_dte=None))
    hits = [t for t in result["trades"] if t["exit_reason"].startswith("profit target")]
    assert hits, "no trade reached the profit target"
    for t in hits:
        # P/L before the exit commission must have cleared the threshold.
        assert t["pl"] + t["commission"] / 2 >= target * abs(t["entry_basis"]) - 0.01


def test_disabling_exits_lets_trades_run_to_expiry(bars):
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", profit_target_pct=None,
        stop_loss_pct=None, close_at_dte=None, target_dte=45))
    reasons = {t["exit_reason"] for t in result["trades"]}
    assert reasons <= {"expired", "end of backtest"}


def test_close_at_dte_caps_holding_time(bars):
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", target_dte=45, close_at_dte=21,
        profit_target_pct=None, stop_loss_pct=None))
    for t in result["trades"]:
        if t["exit_reason"].endswith("DTE"):
            assert t["days_held"] <= 45 - 21 + 4      # allow for weekends


# --------------------------------------------------------------------------
# Concurrency and cadence
# --------------------------------------------------------------------------

def test_single_concurrency_never_overlaps_trades(bars):
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", max_concurrent=1))
    trades = sorted(result["trades"], key=lambda t: t["entry_date"])
    for a, b in zip(trades, trades[1:]):
        assert b["entry_date"] >= a["exit_date"], "positions overlapped"


def test_higher_concurrency_opens_more_trades(bars):
    one = run_backtest(bars, BacktestRules(
        template_id="short_strangle", max_concurrent=1, entry_every_days=5))
    three = run_backtest(bars, BacktestRules(
        template_id="short_strangle", max_concurrent=3, entry_every_days=5))
    assert three["metrics"]["trades"] > one["metrics"]["trades"]


def test_entry_cadence_is_respected(bars):
    cadence = 21
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", entry_every_days=cadence, max_concurrent=5))
    entries = sorted(dt.date.fromisoformat(t["entry_date"]) for t in result["trades"])
    for a, b in zip(entries, entries[1:]):
        assert (b - a).days >= cadence


# --------------------------------------------------------------------------
# Earnings avoidance
# --------------------------------------------------------------------------

def test_skip_earnings_reduces_entries(bars):
    dates = [b.t.date() for b in bars]
    # An announcement every ~90 days across the whole window.
    events = dates[60::63]

    without = run_backtest(bars, BacktestRules(
        template_id="short_strangle", skip_earnings=False),
        earnings_dates=events)
    with_skip = run_backtest(bars, BacktestRules(
        template_id="short_strangle", skip_earnings=True),
        earnings_dates=events)

    assert with_skip["skipped_for_earnings"] > 0
    assert with_skip["metrics"]["trades"] <= without["metrics"]["trades"]


def test_no_trade_spans_an_announcement_when_skipping(bars):
    dates = [b.t.date() for b in bars]
    events = set(dates[60::63])
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", skip_earnings=True, target_dte=45),
        earnings_dates=list(events))

    for t in result["trades"]:
        entry = dt.date.fromisoformat(t["entry_date"])
        horizon = entry + dt.timedelta(days=int(t["dte_at_entry"]))
        assert not any(entry <= e <= horizon for e in events), (
            f"trade opened {entry} spans an announcement")


# --------------------------------------------------------------------------
# Quote sources
# --------------------------------------------------------------------------

def test_modelled_source_is_flagged_and_carries_a_caveat(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    assert result["quote_source"] == "modelled"
    assert result["is_historical_pricing"] is False
    assert "not historical quotes" in result["caveat"]
    assert "rules" in result["caveat"].lower()


def test_stored_source_is_flagged_as_historical(bars):
    dates = [b.t.date() for b in bars]
    modelled = ModelledQuoteSource(
        closes_by_date={d: b.c for d, b in zip(dates, bars)},
        ordered_dates=dates)
    stored = StoredQuoteSource(quotes={}, fallback=modelled)

    result = run_backtest(bars, BacktestRules(template_id="short_strangle"),
                          quote_source=stored)
    assert result["is_historical_pricing"] is True
    assert "historical option quotes" in result["caveat"]


def test_stored_source_falls_back_and_tracks_coverage(bars):
    dates = [b.t.date() for b in bars]
    modelled = ModelledQuoteSource(
        closes_by_date={d: b.c for d, b in zip(dates, bars)},
        ordered_dates=dates)
    stored = StoredQuoteSource(quotes={}, fallback=modelled)

    run_backtest(bars, BacktestRules(template_id="short_strangle"),
                 quote_source=stored)
    assert stored.misses > 0
    assert stored.coverage == 0.0        # empty archive, everything fell back


def test_variance_risk_premium_moves_short_premium_results(bars):
    """
    The implied/realized ratio is the most consequential assumption in a
    modelled backtest. If raising it did not improve short-premium results, the
    parameter would not be doing what it claims.
    """
    low = run_backtest(bars, BacktestRules(
        template_id="short_strangle", variance_risk_premium=1.0))
    high = run_backtest(bars, BacktestRules(
        template_id="short_strangle", variance_risk_premium=1.5))
    assert high["metrics"]["total_pl"] > low["metrics"]["total_pl"]


# --------------------------------------------------------------------------
# Multi-expiry correctness
# --------------------------------------------------------------------------

def test_calendar_legs_age_on_their_own_clocks(bars):
    """
    A calendar's back leg must not be valued as though it expires with the
    front. If it were, every calendar would show a near-total loss at the front
    expiry instead of a tent-shaped payoff.
    """
    result = run_backtest(bars, BacktestRules(
        template_id="call_calendar", target_dte=45, close_at_dte=7))
    assert result["metrics"]["trades"] > 0
    assert result["metrics"]["average_days_held"] > 5

    # A total wipeout on every trade would be the signature of the bug.
    total_loss = [t for t in result["trades"]
                  if t["pl"] < -0.95 * abs(t["entry_basis"])]
    assert len(total_loss) < result["metrics"]["trades"]


def test_degenerate_exit_rule_is_flagged(bars):
    """
    close_at_dte >= the structure's nearest expiry makes every trade exit on day
    one. That is a rules artefact and has to be called out, not served as a
    result.
    """
    result = run_backtest(bars, BacktestRules(
        template_id="short_strangle", target_dte=20, close_at_dte=21))
    assert result["warnings"]
    assert "exited almost immediately" in result["warnings"][0]


# --------------------------------------------------------------------------
# Determinism and structure
# --------------------------------------------------------------------------

def test_backtest_is_deterministic(bars):
    a = run_backtest(bars, BacktestRules(template_id="iron_condor"))
    b = run_backtest(bars, BacktestRules(template_id="iron_condor"))
    assert a["metrics"] == b["metrics"]
    assert len(a["trades"]) == len(b["trades"])


def test_equity_curve_covers_every_evaluated_day(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    assert len(result["equity_curve"]) == result["trading_days"]
    dates = [row["date"] for row in result["equity_curve"]]
    assert dates == sorted(dates)


def test_equity_ends_at_realized_plus_open(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    last = result["equity_curve"][-1]
    assert last["equity"] == pytest.approx(last["realized"] + last["unrealized"],
                                           abs=0.01)


def test_too_little_history_is_rejected_clearly():
    with pytest.raises(ValueError, match="at least"):
        run_backtest(make_bars(40), BacktestRules())


def test_result_is_json_safe(bars):
    import json

    result = run_backtest(bars, BacktestRules(template_id="pmcc"))
    text = json.dumps(result)
    assert "Infinity" not in text and "NaN" not in text


@pytest.mark.parametrize("template", [
    "short_strangle", "iron_condor", "bull_put_spread", "long_call",
    "covered_call", "call_calendar", "pmcc", "short_put",
])
def test_representative_templates_all_backtest(bars, template):
    result = run_backtest(bars, BacktestRules(template_id=template, target_dte=45))
    assert result["metrics"]["trades"] > 0
    assert math.isfinite(result["metrics"]["total_pl"])


# --------------------------------------------------------------------------
# Distribution
# --------------------------------------------------------------------------

def test_distribution_buckets_count_every_trade(bars):
    result = run_backtest(bars, BacktestRules(template_id="short_strangle"))
    dist = pl_distribution(result["trades"], buckets=10)
    assert sum(dist["counts"]) == len(result["trades"])
    assert len(dist["edges"]) == len(dist["counts"]) + 1
    assert dist["min"] <= dist["max"]


def test_distribution_handles_empty_and_degenerate_input():
    assert pl_distribution([])["counts"] == []
    single = pl_distribution([{"pl": 5.0}, {"pl": 5.0}])
    assert sum(single["counts"]) == 2
