"""
The paper account's full lifecycle, end to end through the API.

This is the "testing account" the rest of the app hangs off: the Strategy Lab
opens structures into it, the terminal's ticket fills into it, the Portfolio
page marks it, and closes value the exit. Each hop is tested through the HTTP
surface because that is the surface the UI binds to.

The property running through all of it: paper numbers must be honest about
being paper. Fills are theoretical mids with the method named; closes record
where the exit was valued and how; nothing invents a cash balance.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _open_ids(client):
    return {p["id"] for p in client.get("/api/portfolio/positions").json()["positions"]}


# ------------------------------------------------------------ paper ticket

def test_paper_order_fills_at_a_server_side_price_and_names_the_method(client):
    r = client.post("/api/portfolio/paper-order",
                    json={"symbol": "AAPL", "side": "BUY", "quantity": 25})
    assert r.status_code == 200
    body = r.json()
    try:
        assert body["fill"]["price"] > 0
        assert body["fill"]["method"] in ("mid of the live quote",
                                          "last trade (no two-sided quote)")
        # The honesty note must travel with the fill, not live in a docstring.
        assert "slippage" in body["note"]
        leg = body["position"]["legs"][0]
        assert leg["kind"] == "stock" and leg["quantity"] == 25
        assert leg["entry_price"] == body["fill"]["price"]
    finally:
        client.delete(f"/api/portfolio/positions/{body['position']['id']}")


def test_sell_ticket_opens_a_short_position(client):
    r = client.post("/api/portfolio/paper-order",
                    json={"symbol": "SPY", "side": "SELL", "quantity": 10})
    assert r.status_code == 200
    body = r.json()
    try:
        assert body["position"]["legs"][0]["quantity"] == -10
    finally:
        client.delete(f"/api/portfolio/positions/{body['position']['id']}")


def test_zero_and_negative_quantities_are_rejected(client):
    for qty in (0, -5):
        r = client.post("/api/portfolio/paper-order",
                        json={"symbol": "AAPL", "side": "BUY", "quantity": qty})
        assert r.status_code == 422


# ------------------------------------------------------------------ close

def test_close_without_exit_values_the_position_and_says_how(client):
    opened = client.post("/api/portfolio/paper-order",
                         json={"symbol": "NVDA", "side": "BUY", "quantity": 50}).json()
    pid = opened["position"]["id"]
    fill = opened["fill"]["price"]
    try:
        r = client.post(f"/api/portfolio/positions/{pid}/close")
        assert r.status_code == 200
        closed = r.json()
        assert closed["exit_credit"] is not None
        assert "theoretical mark" in closed["exit_method"]
        # Realized P&L must reconcile: exit value minus entry cost.
        assert closed["realized"] == pytest.approx(
            closed["exit_credit"] - fill * 50, abs=0.5)
        # A closed position leaves the open book.
        assert pid not in _open_ids(client)
        # The mark series ends with a closing mark.
        marks = client.get(f"/api/portfolio/positions/{pid}/marks").json()["marks"]
        assert marks and marks[-1].get("closing") is True
    finally:
        client.delete(f"/api/portfolio/positions/{pid}")


def test_closing_mark_is_recorded_and_flagged(client):
    """Regression: closing a position must persist a final, flagged closing mark.

    The close route already asks the store to record a closing mark, but the
    PositionMark model had no ``closing`` column, so the write raised and was
    silently swallowed -- the mark series came back empty and the equity curve
    had no record of where the position ended. The final mark must survive and
    be flagged so the series ends where the position did.
    """
    opened = client.post("/api/portfolio/paper-order",
                         json={"symbol": "MSFT", "side": "BUY", "quantity": 20}).json()
    pid = opened["position"]["id"]
    try:
        r = client.post(f"/api/portfolio/positions/{pid}/close")
        assert r.status_code == 200
        marks = client.get(f"/api/portfolio/positions/{pid}/marks").json()["marks"]
        assert marks, "closing a position must record at least one mark"
        assert marks[-1].get("closing") is True, \
            "the final mark must be flagged as a closing mark"
    finally:
        client.delete(f"/api/portfolio/positions/{pid}")


def test_close_with_explicit_exit_uses_it_verbatim(client):
    opened = client.post("/api/portfolio/paper-order",
                         json={"symbol": "AAPL", "side": "BUY", "quantity": 10}).json()
    pid = opened["position"]["id"]
    try:
        r = client.post(f"/api/portfolio/positions/{pid}/close?exit_credit=1234.5")
        assert r.status_code == 200
        closed = r.json()
        assert closed["exit_credit"] == 1234.5
        assert closed["exit_method"] == "caller-supplied exit value"
    finally:
        client.delete(f"/api/portfolio/positions/{pid}")


def test_closing_twice_is_a_404_not_a_second_exit(client):
    opened = client.post("/api/portfolio/paper-order",
                         json={"symbol": "SPY", "side": "BUY", "quantity": 5}).json()
    pid = opened["position"]["id"]
    try:
        assert client.post(f"/api/portfolio/positions/{pid}/close").status_code == 200
        # A second close must not silently re-value the exit.
        assert client.post(f"/api/portfolio/positions/{pid}/close").status_code == 404
    finally:
        client.delete(f"/api/portfolio/positions/{pid}")


# ------------------------------------------------- lab-shaped structures

def test_a_lab_structure_round_trips_through_risk(client):
    """The exact payload the Lab's 'Paper trade' button sends."""
    created = client.post("/api/portfolio/positions", json={
        "symbol": "NVDA", "name": "Iron Condor (lab)",
        "legs": [
            {"kind": "put", "quantity": 1, "strike": 140, "dte": 45,
             "entry_price": 1.2, "iv": 0.5},
            {"kind": "put", "quantity": -1, "strike": 145, "dte": 45,
             "entry_price": 1.9, "iv": 0.5},
            {"kind": "call", "quantity": -1, "strike": 205, "dte": 45,
             "entry_price": 1.7, "iv": 0.5},
            {"kind": "call", "quantity": 1, "strike": 210, "dte": 45,
             "entry_price": 1.1, "iv": 0.5},
        ],
        "notes": "opened from Strategy Lab at theoretical chain prices",
    })
    assert created.status_code == 200
    pid = created.json()["id"]
    try:
        risk = client.get("/api/portfolio/risk").json()
        row = next(r for r in risk["detail"] if r["id"] == pid)
        assert row["risk_defined"] is True
        assert row["max_loss"] is not None
        assert risk["greeks"] is not None
    finally:
        client.delete(f"/api/portfolio/positions/{pid}")
