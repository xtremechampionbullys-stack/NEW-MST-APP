"""
The algo gateway: auth, the risk gate, order semantics, webhooks, telemetry.

The single property underneath every test here: there is exactly one execution
path, and the gate sits on it. Order, smart order, basket leg, split chunk,
webhook signal and approved pending order all converge on the same function,
so a limit that holds in one test holds for all of them — and the tests for
the gate itself are the load-bearing ones.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import OptionContract
from app.services import algo_gateway as gw
from app.services.options_levels import gex, max_pain


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_gateway():
    """
    Gateway state is process-local by design; tests must not share it. Each
    test gets clean singletons, and positions created during a test are
    removed after it so the paper book does not leak between tests either.
    """
    gw.keyring = gw.KeyRing()
    gw.gate = gw.RiskGate()
    gw.strategies = gw.StrategyBook()
    gw.pending_orders.clear()
    gw.events.clear()
    yield


@pytest.fixture()
def key(client):
    return client.post("/api/algo/keys", json={"name": "test"}).json()["key"]


def _cleanup_positions(client):
    for p in client.get("/api/portfolio/positions").json()["positions"]:
        client.delete(f"/api/portfolio/positions/{p['id']}")


@pytest.fixture(autouse=True)
def clean_book(client):
    _cleanup_positions(client)
    yield
    _cleanup_positions(client)


def order(client, key, symbol="AAPL", side="BUY", qty=10, mode="auto"):
    return client.post("/api/algo/order",
                       headers={"X-API-Key": key},
                       json={"symbol": symbol, "side": side,
                             "quantity": qty, "mode": mode})


# -------------------------------------------------------------------- auth

def test_orders_require_a_key(client):
    r = client.post("/api/algo/order",
                    json={"symbol": "AAPL", "side": "BUY", "quantity": 1})
    assert r.status_code == 401
    assert "No active keys exist yet" in r.json()["detail"]


def test_a_revoked_key_stops_working(client, key):
    assert order(client, key).json()["status"] == "filled"
    kid = client.get("/api/algo/keys").json()["keys"][0]["id"]
    client.delete(f"/api/algo/keys/{kid}")
    assert order(client, key).status_code == 401


def test_keys_are_stored_hashed_and_shown_once(client):
    created = client.post("/api/algo/keys", json={"name": "bot"}).json()
    assert created["key"].startswith("msk_")
    listed = client.get("/api/algo/keys").json()["keys"][0]
    assert "key" not in listed and "digest" not in listed
    assert created["key"].startswith(listed["prefix"])


# ---------------------------------------------------------------- the gate

def test_order_value_cap_rejects_and_does_not_consume_budget(client, key):
    before = client.get("/api/algo/gate").json()["orders_today"]
    r = order(client, key, symbol="NVDA", qty=50000)   # way past $25k default
    assert r.json()["status"] == "rejected"
    assert "per-order cap" in r.json()["reason"]
    assert client.get("/api/algo/gate").json()["orders_today"] == before


def test_position_cap_counts_what_is_already_held(client, key):
    # Two orders that individually pass but together breach the position cap.
    client.put("/api/algo/gate", json={
        "max_order_value": 25000, "max_position_value": 30000,
        "max_orders_per_day": 50, "daily_loss_limit": 1000,
        "require_approval": False})
    assert order(client, key, qty=100).json()["status"] == "filled"
    r = order(client, key, qty=100)                    # ~$46k held after
    assert r.json()["status"] == "rejected"
    assert "per-symbol cap" in r.json()["reason"]


def test_daily_loss_circuit_breaker_halts_orders(client, key):
    gw.gate.record_realized(-1500.0)                   # past the $1k default
    r = order(client, key)
    assert r.json()["status"] == "rejected"
    assert "circuit breaker" in r.json()["reason"]


def test_order_count_cap(client, key):
    client.put("/api/algo/gate", json={
        "max_order_value": 25000, "max_position_value": 500000,
        "max_orders_per_day": 2, "daily_loss_limit": 1000,
        "require_approval": False})
    assert order(client, key, qty=1).json()["status"] == "filled"
    assert order(client, key, qty=1).json()["status"] == "filled"
    r = order(client, key, qty=1)
    assert r.json()["status"] == "rejected"
    assert "order count limit" in r.json()["reason"]


def test_validate_mode_books_nothing_and_consumes_nothing(client, key):
    before_orders = client.get("/api/algo/gate").json()["orders_today"]
    before_book = client.get("/api/portfolio/positions").json()["count"]
    r = order(client, key, mode="validate")
    assert r.json()["status"] == "validated"
    assert r.json()["would_fill"] > 0
    assert client.get("/api/portfolio/positions").json()["count"] == before_book
    assert client.get("/api/algo/gate").json()["orders_today"] == before_orders


# ------------------------------------------------------------ smart orders

def test_smart_order_computes_the_delta(client, key):
    order(client, key, qty=10)
    r = client.post("/api/algo/smart-order", headers={"X-API-Key": key},
                    json={"symbol": "AAPL", "target_quantity": 30})
    body = r.json()
    assert body["computed_order"] == "BUY 20"
    assert body["held_before"] == 10


def test_smart_order_at_target_is_a_noop_not_an_error(client, key):
    order(client, key, qty=25)
    r = client.post("/api/algo/smart-order", headers={"X-API-Key": key},
                    json={"symbol": "AAPL", "target_quantity": 25})
    assert r.json()["status"] == "noop"


def test_smart_order_flattens_through_zero(client, key):
    order(client, key, qty=10)
    r = client.post("/api/algo/smart-order", headers={"X-API-Key": key},
                    json={"symbol": "AAPL", "target_quantity": -10})
    assert r.json()["computed_order"] == "SELL 20"


# --------------------------------------------------------- basket / split

def test_basket_legs_execute_independently(client, key):
    r = client.post("/api/algo/basket", headers={"X-API-Key": key}, json={
        "orders": [
            {"symbol": "AAPL", "side": "BUY", "quantity": 5, "mode": "auto"},
            {"symbol": "NVDA", "side": "BUY", "quantity": 50000, "mode": "auto"},
            {"symbol": "SPY", "side": "BUY", "quantity": 5, "mode": "auto"},
        ]})
    body = r.json()
    assert body["filled"] == 2 and body["rejected"] == 1
    assert [o["status"] for o in body["orders"]] == ["filled", "rejected", "filled"]


def test_split_chunks_correctly_and_stops_on_rejection(client, key):
    r = client.post("/api/algo/split", headers={"X-API-Key": key},
                    json={"symbol": "AAPL", "side": "BUY",
                          "quantity": 25, "chunk": 10})
    chunks = r.json()["chunks"]
    assert len(chunks) == 3                       # 10 + 10 + 5
    assert all(c["status"] == "filled" for c in chunks)

    # Now with a tight position cap: the ladder must stop at the rejection.
    client.put("/api/algo/gate", json={
        "max_order_value": 25000, "max_position_value": 10000,
        "max_orders_per_day": 50, "daily_loss_limit": 1000,
        "require_approval": False})
    r2 = client.post("/api/algo/split", headers={"X-API-Key": key},
                     json={"symbol": "NVDA", "side": "BUY",
                           "quantity": 300, "chunk": 50})
    statuses = [c["status"] for c in r2.json()["chunks"]]
    assert "rejected" in statuses
    assert statuses.index("rejected") == len(statuses) - 1   # nothing after it


# ---------------------------------------------------------------- approval

def test_semi_auto_queues_and_approval_fills_at_approval_time(client, key):
    client.put("/api/algo/gate", json={
        "max_order_value": 25000, "max_position_value": 50000,
        "max_orders_per_day": 50, "daily_loss_limit": 1000,
        "require_approval": True})
    r = order(client, key, qty=5)
    assert r.json()["status"] == "pending_approval"
    oid = r.json()["order_id"]
    assert client.get("/api/portfolio/positions").json()["count"] == 0

    approved = client.post(f"/api/algo/pending/{oid}/approve").json()
    assert approved["status"] == "filled"
    assert client.get("/api/portfolio/positions").json()["count"] == 1
    # The queue entry is consumed: approving twice is a 404.
    assert client.post(f"/api/algo/pending/{oid}/approve").status_code == 404


def test_rejected_pending_order_never_fills(client, key):
    client.put("/api/algo/gate", json={
        "max_order_value": 25000, "max_position_value": 50000,
        "max_orders_per_day": 50, "daily_loss_limit": 1000,
        "require_approval": True})
    oid = order(client, key, qty=5).json()["order_id"]
    client.post(f"/api/algo/pending/{oid}/reject")
    assert client.get("/api/portfolio/positions").json()["count"] == 0


# ---------------------------------------------------------------- webhooks

def make_strategy(client, **over):
    body = {"name": "tv", "symbol": "SPY", "quantity": 10, "mode": "both"}
    body.update(over)
    return client.post("/api/algo/strategies", json=body).json()


def test_webhook_wrong_secret_is_403(client):
    s = make_strategy(client)
    r = client.post(f"/api/algo/webhook/{s['id']}",
                    json={"secret": "wrong", "action": "BUY"})
    assert r.status_code == 403


def test_webhook_fills_through_the_same_gate(client):
    s = make_strategy(client, quantity=50000)      # breaches order-value cap
    r = client.post(f"/api/algo/webhook/{s['id']}",
                    json={"secret": s["secret"], "action": "BUY"})
    assert r.json()["status"] == "rejected"        # the gate applies to webhooks too


def test_paused_strategy_ignores_signals(client):
    s = make_strategy(client)
    client.patch(f"/api/algo/strategies/{s['id']}?active=false")
    r = client.post(f"/api/algo/webhook/{s['id']}",
                    json={"secret": s["secret"], "action": "BUY"})
    assert r.json()["status"] == "ignored"
    assert client.get("/api/portfolio/positions").json()["count"] == 0


def test_long_only_strategy_blocks_a_naked_sell(client):
    s = make_strategy(client, mode="long")
    r = client.post(f"/api/algo/webhook/{s['id']}",
                    json={"secret": s["secret"], "action": "SELL"})
    assert r.json()["status"] == "blocked"


def test_exit_flattens_whatever_is_held(client):
    s = make_strategy(client, quantity=20)
    client.post(f"/api/algo/webhook/{s['id']}",
                json={"secret": s["secret"], "action": "BUY"})
    r = client.post(f"/api/algo/webhook/{s['id']}",
                    json={"secret": s["secret"], "action": "EXIT"})
    assert r.json()["status"] == "filled"
    # Book holds two positions (entry + exit) netting to zero shares.
    positions = client.get("/api/portfolio/positions").json()["positions"]
    net = sum(l["quantity"] for p in positions for l in p["legs"]
              if p["symbol"] == "SPY")
    assert net == 0


def test_signals_are_logged_on_the_strategy(client):
    s = make_strategy(client)
    client.post(f"/api/algo/webhook/{s['id']}",
                json={"secret": s["secret"], "action": "BUY"})
    listed = client.get("/api/algo/strategies").json()["strategies"][0]
    assert listed["signal_count"] == 1
    assert listed["signals"][0]["result"] == "filled"
    assert "secret" not in listed                   # never in list responses


# --------------------------------------------------------------- telemetry

def test_traffic_groups_by_route_template(client, key):
    order(client, key, symbol="AAPL")
    order(client, key, symbol="NVDA")
    summary = client.get("/api/algo/traffic").json()
    paths = [e["path"] for e in summary["endpoints"]]
    assert "/api/algo/order" in paths
    assert not any("AAPL" in p or "NVDA" in p for p in paths)


# ---------------------------------------------------------- options levels

def _contract(right, strike, oi, gamma=0.01, exp=dt.date(2026, 9, 18)):
    return OptionContract(symbol=f"T{strike}{right[0]}", underlying="T",
                          expiration=exp, strike=strike, right=right,
                          open_interest=oi, gamma=gamma)


def test_max_pain_picks_the_minimum_payout_strike():
    """
    Heavy call OI above 100 and heavy put OI below 100: settling at 100 pays
    neither side, so 100 must be the max-pain strike.
    """
    exp = dt.date(2026, 9, 18)
    chain = [
        _contract("call", 100, 5000), _contract("call", 105, 4000),
        _contract("call", 110, 3000),
        _contract("put", 100, 5000), _contract("put", 95, 4000),
        _contract("put", 90, 3000),
    ]
    out = max_pain(chain, exp)
    assert out["max_pain"] == 100
    assert out["total_call_oi"] == 12000
    assert out["total_put_oi"] == 12000


def test_gex_sign_convention_calls_positive_puts_negative():
    exp = dt.date(2026, 9, 18)
    calls_only = gex([_contract("call", 100, 1000)], exp, spot=100.0)
    puts_only = gex([_contract("put", 100, 1000)], exp, spot=100.0)
    assert calls_only["net_gex"] > 0
    assert puts_only["net_gex"] < 0
    # The assumption is IN the payload, not only in a docstring.
    assert "long calls and short puts" in calls_only["assumption"]
