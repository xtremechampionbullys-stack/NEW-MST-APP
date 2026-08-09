"""
Response-shape contract tests.

Request types are generated from the OpenAPI schema, so a request that the
backend would reject usually fails at compile time in the web app. Responses
get no such protection: most endpoints here build plain dicts, which OpenAPI
describes as `object` and generation therefore types as `unknown`. Renaming
`win_rate` to `winRate` in a router would pass every unit test, pass type
generation, pass `tsc`, deploy cleanly, and render a blank panel.

So this file is the other half of the contract. For each endpoint it asserts
the keys the web app actually reads, and their types.

Two deliberate design choices:

* **Additive changes pass.** Only removals, renames and type changes fail.
  Adding a field should never break a build.
* **The lists are hand-written, not snapshotted.** A snapshot that
  regenerates on failure records whatever the code does, which means it can
  never detect an accidental change -- it just rubber-stamps it.

These run against the synthetic provider, so they exercise shape, not market
correctness. That is the point: shape is what the UI binds to.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


SYM = "AAPL"


# --------------------------------------------------------------------------
# Shape checking
# --------------------------------------------------------------------------

TYPES = {
    "str": str, "num": (int, float), "bool": bool,
    "list": list, "dict": dict, "any": object,
}


def require(payload, spec: dict, path: str = "$"):
    """
    Assert every key in `spec` exists on `payload` with the declared type.

    A spec value is either a type name from TYPES, a nested dict (recurse), or
    a one-element list holding the spec for each item of a list field. Suffix a
    type with "?" to allow null -- used where the engine deliberately returns
    None to mean "not resolvable", which is a real state the UI renders.
    """
    assert isinstance(payload, dict), f"{path} is {type(payload).__name__}, not an object"
    for key, want in spec.items():
        assert key in payload, f"{path}.{key} is missing from the response"
        value = payload[key]

        if isinstance(want, dict):
            require(value, want, f"{path}.{key}")
        elif isinstance(want, list):
            assert isinstance(value, list), f"{path}.{key} is not a list"
            if value:
                item_spec = want[0]
                if isinstance(item_spec, dict):
                    require(value[0], item_spec, f"{path}.{key}[0]")
                else:
                    _scalar(value[0], item_spec, f"{path}.{key}[0]")
        else:
            _scalar(value, want, f"{path}.{key}")


def _scalar(value, want: str, path: str):
    optional = want.endswith("?")
    name = want.rstrip("?")
    if value is None:
        assert optional, f"{path} is null but the contract requires {name}"
        return
    expected = TYPES[name]
    if name == "num":
        assert isinstance(value, expected) and not isinstance(value, bool), \
            f"{path} is {type(value).__name__}, not a number"
    else:
        assert isinstance(value, expected), \
            f"{path} is {type(value).__name__}, not {name}"


def test_the_shape_checker_actually_fails():
    """A contract test that cannot fail is worse than no contract test."""
    with pytest.raises(AssertionError, match="missing"):
        require({"a": 1}, {"b": "num"})
    with pytest.raises(AssertionError, match="not a number"):
        require({"a": "1"}, {"a": "num"})
    with pytest.raises(AssertionError, match="requires num"):
        require({"a": None}, {"a": "num"})
    require({"a": None}, {"a": "num?"})


# --------------------------------------------------------------------------
# Health and market data
# --------------------------------------------------------------------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    require(r.json(), {
        "status": "str", "version": "str",
        "providers": "any", "preferred_provider": "str?",
        "store": {"backend": "str", "persistent": "bool"},
        "config": "dict",
    })


def test_quote(client):
    r = client.get(f"/api/market/quote/{SYM}")
    assert r.status_code == 200
    require(r.json(), {
        "symbol": "str", "last": "num", "bid": "num?", "ask": "num?",
        "mid": "num?", "prev_close": "num?", "change": "num?",
        "change_pct": "num?", "volume": "num?", "timestamp": "str",
        "delayed": "bool", "source": "str",
    })


def test_bars(client):
    r = client.get(f"/api/market/bars/{SYM}", params={"days": 120})
    assert r.status_code == 200
    body = r.json()
    require(body, {
        "symbol": "str", "timespan": "str",
        "bars": [{"t": "num", "o": "num", "h": "num", "l": "num",
                  "c": "num", "v": "num"}],
    })
    # `t` is epoch milliseconds, not an ISO string. The charts do arithmetic on
    # it, so a change to string form would not throw -- it would silently
    # produce an unsorted or empty axis.
    assert body["bars"][0]["t"] > 1_000_000_000_000
    assert body["bars"] == sorted(body["bars"], key=lambda b: b["t"]), \
        "bars must be ascending; every chart and indicator assumes it"


def test_chain(client):
    r = client.get(f"/api/market/chain/{SYM}")
    assert r.status_code == 200
    require(r.json(), {
        "underlying": "str", "spot": "num", "source": "str",
        "fetched_at": "str", "synthetic": "bool",
        "expirations": ["str"],
        "contracts": [{
            "symbol": "str", "underlying": "str", "expiration": "str",
            "strike": "num", "right": "str",
            "bid": "num?", "ask": "num?", "mid": "num?", "last": "num?",
            "implied_vol": "num?", "delta": "num?", "gamma": "num?",
            "theta": "num?", "vega": "num?", "rho": "num?",
            "open_interest": "num?", "volume": "num?",
            "multiplier": "num", "exercise_style": "str", "dte": "num",
            "spread": "num?", "spread_pct": "num?",
        }],
    })


def test_chain_right_values_are_the_ones_the_ui_switches_on(client):
    body = client.get(f"/api/market/chain/{SYM}").json()
    rights = {c["right"] for c in body["contracts"]}
    assert rights <= {"call", "put"}, f"unexpected right values: {rights}"
    assert rights == {"call", "put"}, "chain is missing one side entirely"


def test_volatility_surface(client):
    r = client.get(f"/api/market/volatility/{SYM}")
    assert r.status_code == 200
    require(r.json(), {
        "symbol": "str", "spot": "num", "source": "str",
        "atm_iv_30d": "num?", "realized_vol_20d": "num?",
        "realized_vol_60d": "num?", "variance_risk_premium": "num?",
        "iv_rank": {"iv_rank": "num?", "iv_percentile": "num?",
                    "observations": "num", "method": "str", "note": "str"},
        "term_structure": [{"expiration": "str", "dte": "num", "atm_iv": "num?",
                            "expected_move": "num?", "expected_move_pct": "num?"}],
        "skew": [{"strike": "num", "right": "str", "iv": "num?",
                  "delta": "num?", "moneyness": "num"}],
        "skew_expiration": "str?",
    })


def test_iv_rank_always_says_where_it_came_from(client):
    """
    IV rank means one thing when computed from stored history and another when
    estimated cross-sectionally from today's surface. Dropping `method` would
    let the UI present an estimate as a measurement.
    """
    rank = client.get(f"/api/market/volatility/{SYM}").json()["iv_rank"]
    assert rank["method"], "IV rank did not state its provenance"
    assert rank["note"]


# --------------------------------------------------------------------------
# Strategy
# --------------------------------------------------------------------------

def test_templates_list(client):
    r = client.get("/api/strategy/templates")
    assert r.status_code == 200
    body = r.json()
    require(body, {
        "categories": [{"id": "str", "label": "str", "count": "num"}],
        "templates": [{
            "id": "str", "name": "str", "category": "str", "outlook": "str",
            "complexity": "str", "summary": "str", "mechanics": "str",
            "when_to_use": "str", "risk_notes": "str", "management": "str",
            "params": "list", "risk_defined": "bool?", "credit": "bool?",
            "tags": "list",
        }],
    })
    assert len(body["templates"]) >= 30, "template registry shrank unexpectedly"
    assert sum(c["count"] for c in body["categories"]) == len(body["templates"])


def test_analyze(client):
    r = client.post("/api/strategy/analyze", json={
        "symbol": SYM, "template": "short_strangle",
        "include_monte_carlo": True, "include_earnings": True})
    assert r.status_code == 200, r.text
    require(r.json(), {
        "meta": {"data_source": "str", "chain_used": "bool",
                 "warnings": "list", "spot": "num"},
        "template": {"id": "str", "name": "str", "summary": "str",
                     "mechanics": "str", "when_to_use": "str",
                     "risk_notes": "str", "management": "str",
                     "category": "str", "outlook": "str", "complexity": "str"},
        "position": {
            "name": "str", "symbol": "str", "net_debit": "num",
            "net_credit": "num", "pricing_model": "str",
            "legs": [{"label": "str", "kind": "str", "strike": "num?",
                      "dte": "num", "quantity": "num", "iv": "num",
                      "entry_price": "num", "theo": "num",
                      "delta": "num", "gamma": "num", "theta": "num",
                      "vega": "num", "rho": "num", "extrinsic": "num"}],
        },
        "analysis": {
            "max_profit": "num?", "max_loss": "num?",
            "max_profit_unbounded": "bool", "max_loss_unbounded": "bool",
            "risk_defined": "bool", "breakevens": ["num"],
            "probability_of_profit": "num?", "expected_value": "num?",
            "cvar_5pct": "num?", "return_on_capital": "num?",
            "capital": {"estimated_buying_power": "num", "note": "str"},
            "expected_move_1sd": {"low": "num", "high": "num"},
            "blended_iv": "num", "dte": "num", "multi_expiry": "bool",
            "net_greeks": {"delta": "num", "gamma": "num", "theta": "num",
                           "vega": "num", "rho": "num", "vanna": "num",
                           "charm": "num", "vomma": "num",
                           "dollar_delta": "num"},
        },
    })


def test_unbounded_risk_is_flagged_not_encoded_as_a_number(client):
    """
    JSON has no infinity. An unbounded max loss must arrive as null WITH the
    flag set -- if the flag were dropped the UI would render "—" and a naked
    short strangle would look risk-free.
    """
    a = client.post("/api/strategy/analyze",
                    json={"symbol": SYM, "template": "short_strangle"}).json()["analysis"]
    assert a["max_loss_unbounded"] is True
    assert a["max_loss"] is None
    assert a["risk_defined"] is False

    b = client.post("/api/strategy/analyze",
                    json={"symbol": SYM, "template": "iron_condor"}).json()["analysis"]
    assert b["max_loss_unbounded"] is False
    assert isinstance(b["max_loss"], (int, float))
    assert b["risk_defined"] is True


def test_analyze_curves_and_surface_are_renderable(client):
    """
    Every series must be the same length as its x axis. A misaligned series is
    the one charting bug that does not throw -- SVG just draws a wrong shape,
    confidently.
    """
    body = client.post("/api/strategy/analyze",
                       json={"symbol": SYM, "template": "iron_condor",
                             "include_surface": True}).json()
    require(body, {
        "curves": {"prices": ["num"], "expiry": ["num"], "theoretical": "list"},
        "surface": {"prices": ["num"], "days": ["num"], "grid": "list",
                    "min": "num", "max": "num"},
    })

    curves = body["curves"]
    prices = curves["prices"]
    assert len(prices) > 50 and prices == sorted(prices)
    assert len(curves["expiry"]) == len(prices)
    for series in curves["theoretical"]:
        require(series, {"days": "num", "label": "str", "pl": ["num"]},
                "$.curves.theoretical[]")
        assert len(series["pl"]) == len(prices), \
            f"the T+{series['days']} curve is misaligned with the price axis"

    surface = body["surface"]
    assert len(surface["grid"]) == len(surface["days"])
    for row in surface["grid"]:
        assert len(row) == len(surface["prices"]), "surface row width does not match its axis"
    assert surface["min"] <= surface["max"]


def test_analyze_rejects_an_unknown_template(client):
    r = client.post("/api/strategy/analyze",
                    json={"symbol": SYM, "template": "not_a_strategy"})
    assert r.status_code in (400, 404, 422)


def test_guidance(client):
    r = client.post("/api/strategy/guidance",
                    json={"symbol": SYM, "template": "iron_condor"})
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict) and r.json()


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

def test_backtest_run(client):
    r = client.post("/api/backtest/run", json={
        "symbol": SYM, "lookback_days": 500, "persist": False,
        "rules": {"template_id": "short_strangle", "target_dte": 45,
                  "entry_every_days": 7, "profit_target_pct": 0.5,
                  "close_at_dte": 21}})
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "symbol": "str", "template": "str", "template_name": "str",
        "rules": "dict", "start": "str", "end": "str",
        "trading_days": "num",
        "quote_source": "str", "is_historical_pricing": "bool",
        "caveat": "str", "warnings": "list",
        "trades": [{"entry_date": "str", "exit_date": "str",
                    "entry_spot": "num", "exit_spot": "num",
                    "entry_basis": "num", "pl": "num", "exit_reason": "str",
                    "days_held": "num", "dte_at_entry": "num",
                    "dte_at_exit": "num", "is_win": "bool",
                    "max_adverse": "num", "max_favorable": "num",
                    "return_on_basis": "num?", "commission": "num"}],
        "equity_curve": [{"date": "str", "spot": "num", "realized": "num",
                          "unrealized": "num", "equity": "num",
                          "open_positions": "num"}],
        "metrics": {
            "trades": "num", "wins": "num", "losses": "num",
            "win_rate": "num?", "total_pl": "num", "average_pl": "num?",
            "average_win": "num?", "average_loss": "num?",
            "profit_factor": "num?", "expectancy": "num?",
            "max_drawdown": "num", "max_drawdown_pct": "num?",
            "sharpe": "num?", "average_days_held": "num?",
            "max_consecutive_losses": "num", "exposure_pct": "num?",
        },
        "distribution": {"edges": ["num"], "counts": ["num"],
                         "min": "num", "max": "num"},
    })


def test_backtest_never_hides_that_its_prices_are_modelled(client):
    """
    A backtest run on model-generated option prices validates rules, not fills.
    If `quote_source` or `caveat` ever went missing the UI would present a
    modelled equity curve as a historical one.
    """
    body = client.post("/api/backtest/run", json={
        "symbol": SYM, "lookback_days": 400, "persist": False,
        "rules": {"template_id": "short_put"}}).json()
    assert body["quote_source"]
    assert len(body["caveat"]) > 40
    if not body["is_historical_pricing"]:
        assert "model" in body["caveat"].lower()


def test_backtest_metrics_are_internally_consistent(client):
    m = client.post("/api/backtest/run", json={
        "symbol": SYM, "lookback_days": 600, "persist": False,
        "rules": {"template_id": "short_strangle"}}).json()["metrics"]
    assert m["wins"] + m["losses"] <= m["trades"]
    if m["trades"]:
        assert m["win_rate"] == pytest.approx(m["wins"] / m["trades"], abs=1e-6)
    assert m["max_drawdown"] <= 0


# --------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------

def test_forecast_volatility(client):
    r = client.get(f"/api/forecast/volatility/{SYM}")
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "symbol": "str", "bars": "num", "available": "bool", "as_of": "str",
        "implied_now": "num?",
        "har": {"horizon_days": "num", "forecast_vol": "num",
                "coefficients": "dict", "in_sample_r2": "num?",
                "out_of_sample_r2": "num?", "observations": "num",
                "note": "str"},
        "garch": {"omega": "num", "alpha": "num", "beta": "num",
                  "persistence": "num", "long_run_vol": "num",
                  "current_vol": "num", "half_life_days": "num?",
                  "converged": "bool"},
        "projections": [{"date": "str", "days": "num",
                         "projected_vol": "num", "diffusive_vol": "num",
                         "events_in_window": "num",
                         "implied_now": "num?",
                         "implied_minus_projected": "num?"}],
        "note": "str",
    })
    assert body["garch"]["persistence"] < 1.0, "a non-stationary GARCH was served"
    days = [p["days"] for p in body["projections"]]
    assert days == sorted(days)


def test_forecast_direction_always_reports_its_own_skill(client):
    """
    The contract that matters most on this endpoint. The UI must never be able
    to show a probability without the out-of-sample score that qualifies it --
    a bare "68% chance up" with the skill measurement dropped is the single most
    misleading thing this API could emit.
    """
    r = client.get(f"/api/forecast/direction/{SYM}")
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "symbol": "str", "horizon_days": "num",
        "probability_up": "num",
        "base_rate": "num",
        "edge_vs_base_rate": "num",
        "out_of_sample": {
            "accuracy": "num", "base_rate": "num", "edge": "num",
            "auc": "num", "brier": "num", "brier_base_rate": "num",
            "standard_error": "num",
            "statistically_meaningful": "bool", "samples": "num",
        },
        "trustworthy": "bool",
        "verdict": "str",
        "folds": [{"fold": "num", "accuracy": "num", "edge": "num",
                   "train_size": "num", "test_size": "num"}],
    })
    assert body["trustworthy"] == body["out_of_sample"]["statistically_meaningful"], (
        "trustworthy flag drifted from the significance test it mirrors")
    if not body["trustworthy"]:
        assert "no demonstrable directional edge" in body["verdict"]


def test_forecast_cross_asset(client):
    r = client.get(f"/api/forecast/cross-asset/{SYM}")
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "benchmark": {"symbol": "str", "benchmark": "str", "beta": "num",
                      "alpha_annual": "num", "r_squared": "num",
                      "correlation": "num", "total_vol": "num",
                      "systematic_vol": "num", "idiosyncratic_vol": "num",
                      "observations": "num"},
        "propagation": {"benchmark_move": "num", "expected_stock_move": "num",
                        "idiosyncratic_sd": "num", "range_68": ["num"],
                        "range_95": ["num"], "explained_fraction": "num",
                        "note": "str"},
    })
    b = body["benchmark"]
    assert 0.0 <= b["r_squared"] <= 1.0
    # Variance decomposition identity: the two components must reconstruct the
    # total. If they drift apart the "how much of this is the market" panel is
    # showing numbers that do not add up.
    assert (b["systematic_vol"] ** 2 + b["idiosyncratic_vol"] ** 2) == pytest.approx(
        b["total_vol"] ** 2, rel=0.02)
    p = body["propagation"]
    assert p["range_68"][0] < p["expected_stock_move"] < p["range_68"][1]
    assert p["range_95"][0] <= p["range_68"][0]
    assert p["range_95"][1] >= p["range_68"][1]


def test_forecast_index(client):
    r = client.get("/api/forecast/index/SPY")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict) and r.json()


# --------------------------------------------------------------------------
# Forward
# --------------------------------------------------------------------------

def test_forward_simulate(client):
    r = client.post("/api/forward/simulate", json={
        "symbol": SYM, "template": "short_strangle", "paths": 1500})
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "symbol": "str", "template": "str",
        "horizon_days": "num", "paths": "num", "base_vol": "num",
        "entry_basis": "num",
        "config": {"paths": "num", "annual_drift": "num", "tail_df": "num?",
                   "seed": "num"},
        "rules": {"profit_target_pct": "num?", "stop_loss_pct": "num?",
                  "close_at_dte": "num?"},
        "outcomes": {"win_rate": "num", "mean_pl": "num", "median_pl": "num",
                     "p05": "num", "p25": "num", "p75": "num", "p95": "num",
                     "best": "num", "worst": "num",
                     "expected_win": "num?", "expected_loss": "num?"},
        "path_dependent": {"hit_profit_target": "num", "hit_stop_loss": "num",
                           "closed_on_time": "num", "mean_days_held": "num",
                           "median_days_held": "num",
                           "mean_max_adverse": "num",
                           "worst_max_adverse": "num",
                           "mean_max_adverse_on_winners": "num?",
                           "mean_max_favorable": "num"},
        "terminal_prices": {"p05": "num", "p25": "num", "median": "num",
                            "p75": "num", "p95": "num"},
        "fan_chart": {"days": ["num"], "bands": "dict"},
        "pl_histogram": {"edges": ["num"], "counts": ["num"], "below": "num",
                         "above": "num", "min": "num", "max": "num",
                         "clipped": "bool"},
        "note": "str",
        "volatility_source": "str",
        "events_in_window": "list",
    })

    pd_ = body["path_dependent"]
    total = pd_["hit_profit_target"] + pd_["hit_stop_loss"] + pd_["closed_on_time"]
    assert total == pytest.approx(1.0, abs=1e-6), "exit reasons do not partition the paths"

    o = body["outcomes"]
    assert o["worst"] <= o["p05"] <= o["p25"] <= o["median_pl"] <= o["p75"] <= o["p95"] <= o["best"]
    h = body["pl_histogram"]
    # Bins cover the 1st-99th percentile; the tails are reported separately so
    # one catastrophic path cannot flatten the chart. Nothing may go missing.
    assert sum(h["counts"]) + h["below"] + h["above"] == body["paths"]

    fan = body["fan_chart"]
    assert set(fan["bands"]) == {"5", "25", "50", "75", "95"}
    for series in fan["bands"].values():
        assert len(series) == len(fan["days"])


def test_forward_states_where_its_volatility_came_from(client):
    """
    The simulation is only as good as its vol input, and that input is either
    the chain's implied vol or the forecast model's -- a material difference the
    user is entitled to see. This string is the disclosure.
    """
    body = client.post("/api/forward/simulate",
                       json={"symbol": SYM, "template": "short_put"}).json()
    assert len(body["volatility_source"]) > 10
    assert body["base_vol"] > 0


def test_forward_calibration(client):
    r = client.get("/api/forward/calibration")
    assert r.status_code == 200, r.text
    require(r.json(), {
        "closed_paper_trades": "num", "scored": "num", "unscorable": "num",
        "calibration": {"available": "bool"},
    })


def test_a_paper_trade_survives_the_round_trip_into_calibration(client):
    """
    The whole loop: log a prediction, close the trade, see it counted.

    This existed as three endpoints that each worked and a chain that did not --
    the expectation was accepted at /paper, dropped on the floor, and
    /calibration read a field nobody ever wrote. Every response looked fine;
    the report was permanently empty. Only an end-to-end assertion catches that.
    """
    before = client.get("/api/forward/calibration").json()

    opened = client.post("/api/forward/paper", json={
        "symbol": SYM,
        "name": "contract test",
        "template_id": "short_put",
        "legs": [{"kind": "put", "quantity": -1, "strike": 200.0,
                  "dte": 45, "iv": 0.3, "entry_price": 3.10}],
        "expectation": {
            "probability_of_profit": 0.72,
            "expected_value": 41.0,
            "entry_basis": -310.0,
            "max_profit": 310.0,
            "max_loss": None,
            "breakevens": [196.9],
            "expected_move_pct": 0.06,
            "blended_iv": 0.30,
        },
    })
    assert opened.status_code == 200, opened.text
    pid = opened.json()["id"]
    assert opened.json()["expectation"]["probability_of_profit"] == 0.72

    closed = client.post(f"/api/portfolio/positions/{pid}/close",
                         params={"exit_credit": -150.0})
    assert closed.status_code == 200, closed.text

    after = client.get("/api/forward/calibration").json()
    assert after["closed_paper_trades"] == before["closed_paper_trades"] + 1
    assert after["scored"] == before["scored"] + 1, (
        "the closed paper trade did not reach the calibration report")


def test_a_paper_trade_without_a_prediction_is_refused(client):
    """
    A logged trade with no recorded expectation can never be scored, so it can
    only dilute the record. Better to refuse it at the door than to accept it
    and quietly drop it from every report afterwards.
    """
    r = client.post("/api/forward/paper", json={
        "symbol": SYM, "legs": [{"kind": "put", "quantity": -1, "strike": 200.0}],
        "expectation": {"expected_value": 10.0},
    })
    assert r.status_code == 422
    assert "probability_of_profit" in r.text


# --------------------------------------------------------------------------
# Dojo
# --------------------------------------------------------------------------

def test_dojo_search_space(client):
    r = client.get("/api/dojo/search-space")
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "space": {"profit_target_pct": ["num"], "stop_loss_pct": ["num"],
                  "close_at_dte": ["num"], "target_dte": ["num"],
                  "delta": ["num"], "entry_every_days": ["num"]},
        "combinations": "num",
    })
    prod = 1
    for v in body["space"].values():
        prod *= len(v)
    assert body["combinations"] == prod


def test_dojo_run_reports_its_validation(client):
    r = client.post("/api/dojo/run", json={
        "symbol": SYM, "template_id": "short_strangle",
        "variants": 6, "lookback_days": 900})
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "symbol": "str", "template": "str",
        "variants_tried": "num", "search_space_size": "num",
        "train_window": {"start": "str", "end": "str", "bars": "num"},
        "test_window": {"start": "str", "end": "str", "bars": "num"},
        "selected": {"variant": "dict", "train_metrics": "dict",
                     "test_metrics": "dict", "train_score": "num",
                     "test_score": "num"},
        "baseline": {"rules": "dict", "test_metrics": "any",
                     "test_score": "num?"},
        "validation": {"out_of_sample_percentile": "num",
                       "median_variant_test_score": "num",
                       "degradation": "num",
                       "selection_beat_random": "bool",
                       "selection_beat_baseline": "bool"},
        "leaderboard": "list",
        "verdict": "str",
        "caveat": "str",
    })
    # The window fields are the leak guard, rendered side by side in the UI.
    # If they ever overlap the page is presenting a false claim of validation.
    assert body["test_window"]["start"] >= body["train_window"]["end"]
    assert body["validation"]["selection_beat_random"] == (
        body["validation"]["out_of_sample_percentile"] > 60)


# --------------------------------------------------------------------------
# Earnings, research, dashboard, portfolio
# --------------------------------------------------------------------------

def test_earnings(client):
    r = client.get(f"/api/earnings/{SYM}")
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "symbol": "str", "source": "str", "synthetic": "bool",
        "next_event": {"event_date": "str", "session": "str",
                       "date_status": "str", "days_away": "num",
                       "is_upcoming": "bool", "is_confirmed": "bool"},
        "implied_move": {
            "event_date": "str", "days_away": "num",
            "front_expiration": "str", "selection_note": "str",
            "preferred": "str",
            "straddle": {"method": "str", "expected_move_pct": "num?",
                         "expected_move_abs": "num?", "note": "str"},
            "term_structure": {"method": "str", "expected_move_pct": "num?",
                               "event_vol": "num?", "diffusive_vol": "num?",
                               "note": "str"},
        },
        "history": {"count": "num", "moves": "list",
                    "average_abs_move": "num?", "median_abs_move": "num?"},
        "events": "list",
    })


def test_event_vol_decomposition_declines_rather_than_guesses(client):
    """
    When the term structure carries no event premium the decomposition is not
    computable, and the engine returns None with an explanation instead of
    clamping to zero. Both fields must survive to the wire -- a null with no
    note is indistinguishable from a bug.
    """
    im = client.get(f"/api/earnings/{SYM}").json()["implied_move"]
    for method in ("straddle", "term_structure"):
        block = im[method]
        if block.get("event_vol") is None:
            assert block["note"], f"{method} returned no event vol and no reason"


def test_etfs_report_no_earnings_as_a_fact_not_an_error(client):
    """
    SPY having no earnings is a property of the instrument. Serving it as a 502
    made the UI raise a data-outage banner on the most-watched symbol on the
    platform -- an error state for a completely normal one.
    """
    r = client.get("/api/earnings/SPY")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reports_earnings"] is False
    assert body["next_event"] is None
    assert body["events"] == []
    assert body["note"]


def test_stocks_still_report_earnings_true(client):
    body = client.get(f"/api/earnings/{SYM}").json()
    assert body["reports_earnings"] is True


def test_fundamentals(client):
    r = client.get(f"/api/research/fundamentals/{SYM}")
    assert r.status_code == 200, r.text
    require(r.json(), {
        "symbol": "str",
        "profile": {"ticker": "str", "name": "str", "sector": "str?",
                    "market_cap": "num?"},
        "derived": "dict",
    })


def test_dashboard_watchlist(client):
    r = client.get("/api/dashboard/watchlist", params={"symbols": "AAPL,MSFT"})
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "as_of": "str", "symbols": ["str"],
        "rows": [{"symbol": "str", "last": "num?", "change": "num?",
                  "change_pct": "num?", "source": "str",
                  "sparkline": ["num"], "realized_vol_20d": "num?",
                  "atm_iv_30d": "num?", "iv_rank": "num?",
                  "iv_rank_method": "str?",
                  "variance_risk_premium": "num?"}],
        "summary": {"tracked": "num", "errors": "num"},
        "earnings_soon": "list",
    })
    assert len(body["rows"]) == 2


def test_portfolio_risk(client):
    r = client.get("/api/portfolio/risk")
    assert r.status_code == 200, r.text
    # greeks is null when the book is empty -- a real state, not a missing field.
    require(r.json(), {"positions": "num", "greeks": "any?", "backend": "str"})


def test_agents_are_installed(client):
    r = client.get("/api/agents")
    assert r.status_code == 200, r.text
    body = r.json()
    require(body, {
        "count": "num",
        "agents": [{"id": "str", "name": "str", "status": "str",
                    "purpose": "str", "consumes": ["str"], "emits": ["str"],
                    "permissions": ["str"]}],
        "note": "str",
    })
    assert body["agents"], "no preinstalled agents"
    assert body["count"] == len(body["agents"])
    # Every agent declares whether it is running or merely specified. Shipping
    # a spec as though it were a live agent is the one thing this list must not
    # be able to do.
    # "implemented" joined the vocabulary when the risk gate stopped being a
    # spec and became /api/algo/gate — the one promotion this list exists for.
    assert all(a["status"] in {"specified", "implemented", "active", "disabled"}
               for a in body["agents"]), {a["status"] for a in body["agents"]}


# --------------------------------------------------------------------------
# Schema-level guarantees the type generator depends on
# --------------------------------------------------------------------------

def test_openapi_is_generatable(client):
    """
    `scripts/generate-types.sh` reads this. If it 500s or loses operation ids,
    the web app's types silently stop matching the API.
    """
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.")
    ids = [op["operationId"]
           for ops in spec["paths"].values() for op in ops.values()
           if isinstance(op, dict) and "operationId" in op]
    assert len(ids) == len(set(ids)), "duplicate operationIds break type generation"
    assert len(spec["paths"]) >= 45


@pytest.mark.parametrize("path", [
    "/api/strategy/analyze", "/api/backtest/run", "/api/forward/simulate",
    "/api/dojo/run",
])
def test_post_endpoints_declare_a_request_schema(client, path):
    """Every POST the web app calls must have a generatable body type."""
    spec = client.get("/openapi.json").json()
    body = spec["paths"][path]["post"].get("requestBody")
    assert body, f"{path} has no requestBody — its type cannot be generated"
    assert "$ref" in body["content"]["application/json"]["schema"]
