"""
The unified accounts endpoint: the one shape the Portfolio page binds to.

Two properties matter enough to pin down:

* **The paper account is always present and always first.** It is the app's
  own book, and it exists whether or not any broker credentials do. A page
  that shows nothing until a broker is connected would hide the positions the
  Strategy Lab has been opening all along.

* **Absence of a broker is a described state, not an error.** With no
  credentials the endpoint returns 200 with a card that says what connecting
  takes — not a 502, and not an empty list that leaves the UI guessing which
  of "no brokers exist" and "the call failed" it is looking at.

What is deliberately NOT tested here: the Webull-connected path. It requires
real credentials and a live account, and a mock that returns invented balances
would be a test of the mock. The adapter's URI strings are asserted instead —
they were verified against the official SDK source, and a silent path typo
produces a convincing 404 blamed on the broker.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers import webull


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_paper_account_is_always_first(client):
    r = client.get("/api/brokers/accounts")
    assert r.status_code == 200
    accounts = r.json()["accounts"]
    assert accounts, "the paper account must exist even with nothing configured"
    paper = accounts[0]
    assert paper["id"] == "paper"
    assert paper["kind"] == "paper"
    assert paper["connected"] is True
    assert isinstance(paper["open_positions"], int)
    # A paper account must never carry invented balances.
    assert paper["balances"] is None
    assert "balance_note" in paper


def test_unconfigured_broker_is_a_described_state_not_an_error(client):
    r = client.get("/api/brokers/accounts")
    assert r.status_code == 200
    entries = [a for a in r.json()["accounts"] if a["broker"] == "webull"]
    assert entries, "the webull card must appear so the UI can say how to connect"
    wb = entries[0]
    assert wb["connected"] is False
    assert "WEBULL_APP_KEY" in wb["reason"]


def test_paper_account_reflects_positions_added_through_the_api(client):
    before = client.get("/api/brokers/accounts").json()["accounts"][0]["open_positions"]
    created = client.post("/api/portfolio/positions", json={
        "symbol": "AAPL", "name": "test covered call",
        "legs": [
            {"kind": "stock", "quantity": 100, "entry_price": 230.0},
            {"kind": "call", "quantity": -1, "strike": 240.0, "dte": 30,
             "entry_price": 3.5, "iv": 0.3},
        ],
    })
    assert created.status_code == 200
    pid = created.json()["id"]
    try:
        after = client.get("/api/brokers/accounts").json()["accounts"][0]["open_positions"]
        assert after == before + 1
    finally:
        client.delete(f"/api/portfolio/positions/{pid}")


def test_webull_account_uris_match_the_official_sdk():
    """
    The v2 API split account endpoints across two prefixes — list stayed under
    /openapi/account/ while balance and positions moved to /openapi/assets/.
    The JP docs still show the old paths, so this is exactly the kind of thing
    a well-meaning cleanup would "fix" into a 404.
    """
    src_accounts = inspect.getsource(webull.WebullProvider.get_accounts)
    src_balance = inspect.getsource(webull.WebullProvider.get_balance)
    src_positions = inspect.getsource(webull.WebullProvider.get_positions)
    assert '"/openapi/account/list"' in src_accounts
    assert '"/openapi/assets/balance"' in src_balance
    assert '"/openapi/assets/positions"' in src_positions


def test_absent_balance_fields_become_none_not_zero():
    """
    "No cash figure reported" and "zero cash" are different facts about an
    account. The parser must preserve the difference.
    """
    provider = webull.WebullProvider.__new__(webull.WebullProvider)

    async def fake_request(method, uri, *, params=None, body=None, retries=2):
        return {"data": {"account_currency_assets": []}}   # nothing reported

    provider._request = fake_request                        # noqa: SLF001

    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        provider.get_balance("123", "USD"))
    assert out["total_cash"] is None
    assert out["buying_power"] is None
    assert out["total_market_value"] is None
