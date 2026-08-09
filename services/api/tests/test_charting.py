"""
UDF datafeed and market board contracts.

The datafeed is a protocol implementation, and the failure mode of a protocol
implementation is that it returns plausible JSON the client cannot use. The
Charting Library is not in this repo, so these tests stand in for it: they
assert the exact shapes its `datafeeds/udf` adapter reads, including the one
that is easy to get wrong and impossible to notice — `s: "no_data"` with a
`nextTime`, which is what stops the library retrying an empty range forever.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------
# UDF
# --------------------------------------------------------------------------

def test_config_declares_only_what_is_implemented(client):
    cfg = client.get("/api/tv/config").json()
    assert cfg["supported_resolutions"]
    assert cfg["supports_search"] is True
    # Marks and group requests are not implemented. Advertising them puts
    # controls in the library's UI that call endpoints that do not exist.
    assert cfg["supports_marks"] is False
    assert cfg["supports_group_request"] is False


def test_time_is_a_bare_integer(client):
    """UDF `/time` is seconds, not an object. The adapter parses it directly."""
    r = client.get("/api/tv/time")
    assert r.status_code == 200
    assert isinstance(r.json(), int)
    assert r.json() > 1_700_000_000


def test_symbol_resolution_shape(client):
    s = client.get("/api/tv/symbols", params={"symbol": "AAPL"}).json()
    for key in ("name", "ticker", "description", "type", "session", "timezone",
                "exchange", "minmov", "pricescale", "has_intraday",
                "supported_resolutions"):
        assert key in s, f"UDF symbol info is missing {key}"
    assert s["has_intraday"] is False
    assert s["supported_resolutions"] == client.get("/api/tv/config").json()[
        "supported_resolutions"]


def test_synthetic_resolutions_are_labelled_on_the_chart_itself(client):
    """
    The synthetic provider generates a deterministic series for ANY ticker, so
    while it is active there is no such thing as an unknown symbol and a 404 is
    unreachable. A chart of a name that does not exist, drawn without comment,
    is the worst outcome available here — so the admission goes in
    `description`, which is the string the Charting Library prints in its symbol
    header and search results. The user sees it on the chart, not in a config
    endpoint they will never open.
    """
    note = client.get("/api/tv/datafeed-note").json()
    s = client.get("/api/tv/symbols", params={"symbol": "NOTATICKER123"})
    if note["synthetic"]:
        assert s.status_code == 200
        assert "SYNTHETIC" in s.json()["description"]
        assert s.json()["marketshark_synthetic"] is True
        assert "do not exist" in note["note"]
    else:
        assert s.status_code == 404


def test_search_returns_the_adapter_s_shape(client):
    rows = client.get("/api/tv/search", params={"query": "SP", "limit": 5}).json()
    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row) >= {"symbol", "full_name", "description", "exchange", "ticker"}
    assert any(r["symbol"] == "SPY" for r in rows)


def test_history_returns_column_arrays_of_equal_length(client):
    h = client.get("/api/tv/history", params={
        "symbol": "AAPL", "resolution": "1D",
        "from": 1_740_000_000, "to": 2_000_000_000}).json()
    assert h["s"] == "ok"
    n = len(h["t"])
    assert n > 0
    for k in ("o", "h", "l", "c", "v"):
        assert len(h[k]) == n, f"column {k} is a different length from t"
    assert h["t"] == sorted(h["t"]), "bars must be ascending"
    for i in range(n):
        assert h["l"][i] <= h["o"][i] <= h["h"][i]
        assert h["l"][i] <= h["c"][i] <= h["h"][i]


def test_history_outside_the_range_says_no_data_with_a_next_time(client):
    """
    The detail that matters. An empty `t` array reads to the library as "this
    range is still loading" and it retries indefinitely; `no_data` plus
    `nextTime` tells it where data actually begins so it stops.
    """
    h = client.get("/api/tv/history", params={
        "symbol": "AAPL", "resolution": "1D",
        "from": 100_000, "to": 200_000}).json()
    assert h["s"] == "no_data"
    assert "t" not in h


def test_unsupported_resolution_is_refused(client):
    r = client.get("/api/tv/history", params={
        "symbol": "AAPL", "resolution": "5",
        "from": 1_740_000_000, "to": 2_000_000_000})
    assert r.status_code == 400
    assert "resolution" in r.text


# --------------------------------------------------------------------------
# Honesty about what is actually connected
# --------------------------------------------------------------------------

def test_status_does_not_claim_trading_is_wired(client):
    """
    The claim this endpoint exists to prevent. "Add TradingView charts with
    broker integration" describes three different products, only one of which
    is free and none of which puts an order ticket on a chart without
    TradingView's review. The status must say so rather than implying the
    widget carries trading.
    """
    s = client.get("/api/tv/status").json()
    assert s["widget"]["live"] is True
    assert s["widget"]["trading"] is False
    assert s["charting_library"]["live"] is False
    assert s["broker_integration"]["live"] is False
    assert s["broker_integration"]["blocked_on"]


def test_status_points_at_the_datafeed_that_actually_exists(client):
    s = client.get("/api/tv/status").json()
    lib = s["charting_library"]
    assert lib["datafeed_ready"] is True
    # The URL it advertises must actually serve the protocol.
    assert client.get(f"{lib['datafeed_url']}/config").status_code == 200


# --------------------------------------------------------------------------
# Market board
# --------------------------------------------------------------------------

def test_segments_board_shape(client):
    b = client.get("/api/dashboard/segments").json()
    assert b["segments"]
    for seg in b["segments"]:
        assert seg["name"] and seg["rows"]
        for row in seg["rows"]:
            assert {"label", "symbol", "kind", "instrument", "is_proxy"} <= set(row)
    assert b["breadth"]["instruments"] > 0


def test_every_futures_row_admits_it_is_a_proxy(client):
    """
    The substitution this board makes is quoting SPY where a trader reads "ES".
    They track closely during the session and diverge overnight — which is
    exactly when someone opens a futures board. So the admission has to be on
    the row, not buried in a page footnote.
    """
    b = client.get("/api/dashboard/segments").json()
    rows = [r for seg in b["segments"] for r in seg["rows"]]
    proxies = [r for r in rows if r["is_proxy"]]
    assert proxies, "no proxied rows — the board no longer covers the futures complex"
    for r in proxies:
        assert r["proxy_for"], f"{r['symbol']} claims to be a proxy for nothing"
        assert r["instrument"] == "etf_proxy"
    assert b["futures_available"] is False
    assert "proxies" in b["provenance"] or "proxy" in b["provenance"]
    for r in proxies:
        assert r["proxy_for"] in b["provenance"]


def test_relative_strength_is_zero_for_the_benchmark_against_itself(client):
    b = client.get("/api/dashboard/segments").json()
    rows = {r["symbol"]: r for seg in b["segments"] for r in seg["rows"]}
    bench = rows[b["benchmark"]]
    if bench.get("rs_1m_vs_benchmark") is not None:
        assert bench["rs_1m_vs_benchmark"] == pytest.approx(0.0, abs=1e-12)


def test_every_move_column_is_a_fraction_not_a_percentage(client):
    """
    The unit trap this board fell into once. `Quote.change_pct` is a percentage
    number while every other ratio in the codebase is a fraction, and putting
    both on one row produced a -439% day on an ETF. That version was obvious;
    the dangerous version is the reverse, where a real 0.5% move renders as
    0.005% and reads as a quiet session.

    So: every window column on a row is a fraction, and the day column must
    agree with the quote it came from after conversion.
    """
    b = client.get("/api/dashboard/segments").json()
    rows = [r for seg in b["segments"] for r in seg["rows"] if "error" not in r]
    assert rows
    for r in rows:
        assert "change_pct" not in r, (
            f"{r['symbol']} carries change_pct — a percentage number next to "
            f"fraction columns is how the units got mixed the first time")
        for key in ("change_1d", "change_1w", "change_1m", "change_3m"):
            v = r.get(key)
            if v is None:
                continue
            assert abs(v) < 3.0, f"{r['symbol']}.{key} = {v} is not a fraction"

    sample = rows[0]
    quote = client.get(f"/api/market/quote/{sample['symbol']}").json()
    if quote.get("change_pct") is not None and sample.get("change_1d") is not None:
        assert sample["change_1d"] == pytest.approx(quote["change_pct"] / 100.0, abs=1e-9)


def test_breadth_counts_add_up(client):
    b = client.get("/api/dashboard/segments").json()
    br = b["breadth"]
    assert br["advancing"] + br["declining"] == br["instruments"]


# --------------------------------------------------------------------------
# News feed endpoint
# --------------------------------------------------------------------------

def test_news_feed_is_ranked_and_carries_its_components(client):
    b = client.get("/api/dashboard/news",
                   params={"symbols": "NVDA,AAPL,TSLA", "limit": 20}).json()
    assert b["items"]
    scores = [i["scoring"]["impact"] for i in b["items"]
              if i["scoring"]["impact"] is not None]
    assert scores == sorted(scores, reverse=True), "feed is not ranked by impact"
    for i in b["items"]:
        assert "band" in i["scoring"]
        if i["scoring"]["impact"] is not None:
            assert i["scoring"]["components"]["move_sigma"] is not None
    assert "co-movement" in b["limits"]


def test_news_feed_never_reports_impact_without_the_window_it_measured(client):
    b = client.get("/api/dashboard/news",
                   params={"symbols": "NVDA,AAPL", "limit": 20}).json()
    for i in b["items"]:
        if i["scoring"]["impact"] is not None:
            assert i["scoring"]["window"]["reaction_close"]
            assert i["scoring"]["caveat"]
