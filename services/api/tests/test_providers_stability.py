"""
The synthetic series must not depend on how much of it you ask for.

This was a real bug and a quiet one. The generator's length was driven by the
caller's `limit`, so every request size drew a different random walk: 119 of 120
overlapping dates disagreed between a 120-bar and a 400-bar request.

Nothing raised, and no single screen looked wrong. What it broke was
comparability — a 900-day backtest and a 1200-day dojo run on the same name were
scoring rules against two unrelated price histories, and comparing runs is the
entire purpose of the dojo.
"""

from __future__ import annotations

import asyncio

import pytest

from app.providers.synthetic import SyntheticProvider


def _bars(provider, symbol, limit):
    return asyncio.get_event_loop().run_until_complete(
        provider.get_bars(symbol, limit=limit))


@pytest.mark.parametrize("small,large", [(60, 250), (120, 400), (250, 1000)])
def test_overlapping_dates_agree_across_request_sizes(small, large):
    p = SyntheticProvider()
    a = asyncio.run(p.get_bars("NVDA", limit=small))
    b = asyncio.run(p.get_bars("NVDA", limit=large))
    da = {x.t.date(): x for x in a}
    db = {x.t.date(): x for x in b}
    common = sorted(set(da) & set(db))
    assert len(common) >= small - 2, "the two windows barely overlap; test is not measuring anything"
    for d in common:
        assert da[d].c == pytest.approx(db[d].c, abs=1e-9), f"{d} close differs by window size"
        assert da[d].o == pytest.approx(db[d].o, abs=1e-9)
        assert da[d].h == pytest.approx(db[d].h, abs=1e-9)
        assert da[d].l == pytest.approx(db[d].l, abs=1e-9)


def test_the_last_bar_is_the_same_regardless_of_window():
    p = SyntheticProvider()
    lasts = {asyncio.run(p.get_bars("AAPL", limit=n))[-1].c for n in (60, 200, 500, 1200)}
    assert len(lasts) == 1, f"last close depends on window size: {lasts}"


def test_requesting_more_history_only_prepends():
    p = SyntheticProvider()
    short = asyncio.run(p.get_bars("SPY", limit=100))
    long = asyncio.run(p.get_bars("SPY", limit=500))
    assert [b.c for b in long[-100:]] == [b.c for b in short]


def test_two_providers_agree():
    """A fresh instance must not draw a different series — the cache is a
    performance detail, not the source of determinism."""
    a = asyncio.run(SyntheticProvider().get_bars("TSLA", limit=200))
    b = asyncio.run(SyntheticProvider().get_bars("TSLA", limit=200))
    assert [x.c for x in a] == [x.c for x in b]
