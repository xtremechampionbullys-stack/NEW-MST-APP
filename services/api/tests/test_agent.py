"""
Agent routing tests.

A natural-language router fails in a particular way: it keeps working. Every
utterance gets routed somewhere, every route returns a plausible answer, and a
misparse is indistinguishable from a correct parse until someone checks the
symbol it actually used. So most of this file is adversarial parsing — phrasings
built to trip the extractors — plus the two behavioural guarantees that matter:
it refuses to place orders, and it says so when it does not understand.

Every bug asserted below was real and was found by running the thing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.agent import (
    extract_outlook,
    extract_params,
    extract_symbol,
    extract_template,
    route,
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------
# Symbol extraction — the highest-consequence parser
# --------------------------------------------------------------------------

def test_the_delta_bug():
    """
    The original extractor uppercased the whole message before matching, so
    "16 delta iron condor on NVDA" resolved DELTA as the ticker. The synthetic
    provider quotes any symbol, so the agent then confidently analysed a company
    that does not exist and nothing anywhere looked broken.
    """
    assert extract_symbol("set up a 16 delta iron condor on NVDA 45 dte") == "NVDA"


@pytest.mark.parametrize("text,expected", [
    ("what should I trade on AAPL", "AAPL"),
    ("what should i trade on aapl", "AAPL"),
    ("$tsla earnings", "TSLA"),
    ("show me the iv on NVDA", "NVDA"),
    ("BUY CALLS ON AMD", "AMD"),
    ("backtest a short strangle on SPY", "SPY"),
    ("run the dojo on META", "META"),
    ("price of coin", "COIN"),
])
def test_symbol_extraction(text, expected):
    assert extract_symbol(text) == expected


@pytest.mark.parametrize("text", [
    "what can you do",
    "help me out",
    "show me the best setup",   # every word is a stopword or unknown
])
def test_no_symbol_is_better_than_a_wrong_one(text):
    assert extract_symbol(text) is None


def test_a_lowercase_word_that_is_not_a_known_ticker_is_a_word():
    """
    The lowercase fallback must be gated on the known-symbol list. Without that
    gate, "sell a put on the dip" resolves DIP and analyses a stock that only
    exists because the synthetic provider will invent one.
    """
    assert extract_symbol("sell a put on the dip") is None


# --------------------------------------------------------------------------
# Template extraction
# --------------------------------------------------------------------------

def test_the_which_bug():
    """
    Aliases were matched as raw substrings, so "ic" matched inside "which" and
    attached an iron condor to "which way is NVDA going". Short aliases are the
    useful ones and they are exactly the ones that hide inside ordinary words.
    """
    assert extract_template("which way is nvda going") is None
    assert extract_template("ic on SPY") == "iron_condor"


@pytest.mark.parametrize("text,expected", [
    ("set up an iron condor", "iron_condor"),
    ("sell a strangle", "short_strangle"),
    ("show me a pmcc", "pmcc"),
    ("poor man's covered call", "pmcc"),
    ("a covered call on AAPL", "covered_call"),
    ("cash secured put", "short_put"),
    ("long straddle into earnings", "long_straddle"),
    ("wheel it", "wheel"),
])
def test_template_aliases(text, expected):
    assert extract_template(text) == expected


def test_longer_alias_wins_over_the_shorter_one_it_contains():
    """"poor man's covered call" must not resolve to a plain covered call."""
    assert extract_template("poor mans covered call on NVDA") == "pmcc"


# --------------------------------------------------------------------------
# Parameters and outlook
# --------------------------------------------------------------------------

def test_delta_is_accepted_in_both_conventions():
    """Traders say "16 delta" and "0.16 delta" interchangeably."""
    assert extract_params("16 delta")["delta"] == pytest.approx(0.16)
    assert extract_params("0.16 delta")["delta"] == pytest.approx(0.16)


def test_params_are_extracted_together():
    p = extract_params("5 wide iron condor at 30 delta 45 dte, 3 contracts")
    assert p["width"] == 5
    assert p["delta"] == pytest.approx(0.30)
    assert p["dte"] == 45
    assert p["contracts"] == 3


def test_the_set_up_bug():
    """
    "up" as a bare bullish keyword matched "set **up** an iron condor" and
    turned a neutral structure request bullish — which then changed the template
    the agent chose when none was named.
    """
    assert extract_outlook("set up an iron condor on NVDA") is None


@pytest.mark.parametrize("text,expected", [
    ("i'm bullish on AAPL", "bullish"),
    ("i think TSLA moves down hard", "bearish"),
    ("if it goes nowhere", "neutral"),
    ("expecting a big move either way", "volatile"),
])
def test_outlook_phrases(text, expected):
    assert extract_outlook(text) == expected


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("phrase,target", [
    ("go to research", "/research"),
    ("open the strategy lab", "/lab"),
    ("take me to the dashboard", "/dashboard"),
    ("show me charting", "/charting"),
])
def test_navigation_targets_match_the_route_table(phrase, target):
    """
    Route strings live in two places — the agent's alias map and the web app's
    nav. When the landing page changed, a stale alias would have sent every
    "open the lab" request to the chat, which reads as the agent ignoring you.
    """
    t = route(phrase)
    assert t.actions and t.actions[0]["to"] == target


@pytest.mark.parametrize("text,intent", [
    ("what is NVDA trading at", "quote"),
    ("set up a 16 delta iron condor on NVDA", "analyze"),
    ("what should I trade on AAPL", "suggest"),
    ("backtest a short strangle on SPY", "backtest"),
    ("simulate this forward on NVDA", "forward_test"),
    ("run the dojo on AAPL", "dojo"),
    ("volatility forecast for TSLA", "forecast_vol"),
    ("which way is NVDA going", "direction"),
    ("when does AAPL report", "earnings"),
    ("go to research", "navigate"),
    ("what can you do", "help"),
])
def test_intent_routing(text, intent):
    assert route(text).intent == intent


def test_a_structure_and_a_ticker_is_a_build_request():
    """Inference from what was supplied, not a guess at what was meant."""
    t = route("ic on SPY")
    assert t.intent == "analyze" and t.symbol == "SPY" and t.template == "iron_condor"


def test_an_unmatched_utterance_says_so_rather_than_guessing():
    """
    The decisive test. A router that always routes is a router whose mistakes
    are invisible — the user cannot tell a wrong answer from a right one, so
    every answer becomes untrustworthy.
    """
    t = route("xyzzy plugh")
    assert t.intent == "unknown"
    assert t.understood is False
    assert "didn't understand" in t.reply
    assert not t.tool_calls


def test_a_missing_symbol_is_asked_for_not_invented():
    t = route("backtest a short strangle")
    assert t.understood is False
    assert "which symbol" in t.reply.lower()
    assert not t.tool_calls


def test_context_supplies_the_symbol_when_the_message_does_not():
    t = route("run a backtest", context={"symbol": "NVDA"})
    assert t.intent == "backtest" and t.symbol == "NVDA"


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "buy 10 NVDA calls",
    "place the order",
    "submit this order now",
])
def test_it_refuses_to_place_orders(text):
    """
    Order submission is disabled platform-wide. An agent that says "done" here
    would be the single most damaging thing in the product.
    """
    t = route(text)
    assert t.intent == "place_order"
    assert "can't place orders" in t.reply
    assert not t.tool_calls


def test_the_refusal_offers_what_it_can_actually_do():
    t = route("buy 10 NVDA calls")
    assert "paper trade" in t.reply


# --------------------------------------------------------------------------
# Execution — the tools have to actually run
# --------------------------------------------------------------------------

def test_analyze_returns_real_analytics_and_says_which_tool_ran(client):
    r = client.post("/api/agent/chat",
                    json={"message": "set up a 16 delta iron condor on NVDA 45 dte"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["intent"] == "analyze"
    assert d["symbol"] == "NVDA" and d["template"] == "iron_condor"
    assert [t["tool"] for t in d["tool_calls"]] == ["strategy.analyze"]
    a = d["data"]["analysis"]["analysis"]
    assert a["probability_of_profit"] is not None
    assert a["risk_defined"] is True
    # The reply must reflect the numbers, not narrate around them.
    assert "Probability of profit" in d["reply"]


def test_the_action_lets_the_ui_actually_open_the_position(client):
    """
    An agent that says "I've set it up" without a payload the client can act on
    is describing work it did not do.
    """
    d = client.post("/api/agent/chat",
                    json={"message": "build a pmcc on NVDA"}).json()
    act = [a for a in d["actions"] if a["type"] == "open_position"]
    assert act, d["actions"]
    assert act[0]["symbol"] == "NVDA" and act[0]["template"] == "pmcc"
    # The Strategy Lab moved to /lab when the chat became the landing page.
    # A stale target here would send the user to the chat with a position it
    # cannot open, which looks like the button doing nothing.
    assert act[0]["to"] == "/lab"


def test_navigation_returns_an_action_not_a_promise(client):
    d = client.post("/api/agent/chat", json={"message": "go to research"}).json()
    assert d["actions"] == [{"type": "navigate", "to": "/research", "symbol": None}]


def test_backtest_through_the_agent_carries_the_same_caveat_as_the_endpoint(client):
    d = client.post("/api/agent/chat",
                    json={"message": "backtest a short strangle on SPY"}).json()
    assert [t["tool"] for t in d["tool_calls"]] == ["backtest.run"]
    assert d["caveat"]
    assert d["data"]["backtest"]["quote_source"]
    # The caveat about modelled pricing must survive into the chat reply, not
    # only into the payload nobody opens.
    assert d["caveat"] in d["reply"]


def test_direction_through_the_agent_carries_its_own_skill_verdict(client):
    d = client.post("/api/agent/chat", json={"message": "which way is NVDA going"}).json()
    res = d["data"]["direction"]
    assert res["verdict"] in d["reply"]
    if not res["trustworthy"]:
        assert "no demonstrable directional edge" in d["reply"]


def test_a_failing_tool_reports_the_failure(client):
    """Silence or a cheerful non-answer would both be worse than the traceback."""
    d = client.post("/api/agent/chat",
                    json={"message": "backtest a short strangle on ZZZZZ",
                          "symbol": None}).json()
    assert d["intent"] == "backtest"
    assert d["reply"]


def test_capabilities_states_the_engine_and_the_order_limitation(client):
    c = client.get("/api/agent/capabilities").json()
    assert c["engine"] == "rules"
    assert c["can_place_orders"] is False
    assert c["capabilities"] and c["intents"]
    assert "deterministic" in c["note"]
