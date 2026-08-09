"""
Agentic harness tests.

The properties under test are the ones that decide whether a self-improving
agent is learning or just accumulating confidence:

* recall must find substrings, or it under-reports silently;
* a fact must carry how it was learned, and measured facts must decay;
* a skill must refuse to report a success rate it has not earned;
* and demotion must work, because promotion-only is not learning.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agentics import (BuiltinMemoryProvider, Message, MemoryManager, MemoryStore,
                          SessionDB, SkillLibrary, wilson_lower_bound)
from app.agentics.skills import BASELINE, MIN_TRIALS


# ---------------------------------------------------------------- state ---

@pytest.fixture
def db():
    return SessionDB()


def test_messages_round_trip(db):
    sid = db.create_session(user_id="u1")
    db.add_message(sid, Message(role="user", content="hello"))
    db.add_message(sid, Message(role="assistant", content="hi"))
    assert [m["role"] for m in db.messages(sid)] == ["user", "assistant"]
    assert db.get_session(sid)["message_count"] == 2


def test_word_search_finds_whole_terms(db):
    sid = db.create_session()
    db.add_message(sid, Message(role="user", content="run a backtest on SPY"))
    assert db.search("backtest")


def test_trigram_search_finds_substrings_the_word_index_cannot(db):
    """
    The reason the second FTS table exists. With only a unicode61 tokenizer,
    "condor" does not match inside "iron_condor" — recall returns nothing and
    the user has no way to tell that from an honest absence.
    """
    sid = db.create_session()
    db.add_message(sid, Message(role="assistant", content="built an iron_condor on NVDA"))
    assert db.search("condor"), "substring recall failed — the trigram index is not working"
    assert db.search("cond")


def test_search_is_scoped_when_a_session_is_given(db):
    a, b = db.create_session(), db.create_session()
    db.add_message(a, Message(role="user", content="alpha topic"))
    db.add_message(b, Message(role="user", content="alpha topic"))
    assert len(db.search("alpha")) == 2
    assert len(db.search("alpha", session_id=a)) == 1


def test_empty_query_returns_nothing_rather_than_everything(db):
    sid = db.create_session()
    db.add_message(sid, Message(role="user", content="something"))
    assert db.search("") == []


def test_deleting_a_session_cascades_to_messages_and_index(db):
    sid = db.create_session()
    db.add_message(sid, Message(role="user", content="ephemeral content here"))
    db._execute_write(lambda c: c.execute("DELETE FROM sessions WHERE id=?", (sid,)))
    assert db.search("ephemeral") == []


# --------------------------------------------------------------- memory ---

@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path)


def test_a_fact_records_how_it_was_learned(mem):
    f = mem.store("wheel works on AAPL", source="measured", subject="AAPL", run_id="r1")
    assert f.source == "measured" and f.run_id == "r1"
    assert "[measured]" in f.to_line()
    reloaded = mem.load()[0]
    assert reloaded.source == "measured" and reloaded.run_id == "r1"


def test_an_unknown_source_degrades_to_inferred_rather_than_being_trusted(mem):
    assert mem.store("x", source="proven-by-vibes").source == "inferred"


def test_measured_confidence_decays_but_stated_does_not(mem):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)
    measured = mem.store("measured thing", source="measured")
    stated = mem.store("stated thing", source="stated")
    measured.learned_at = old.isoformat()
    stated.learned_at = old.isoformat()
    assert measured.confidence() < 0.3, "a 180-day-old measurement still reads as current"
    assert stated.confidence() == 1.0, "a stated preference should not expire on a timer"


def test_repeating_a_fact_strengthens_it_instead_of_duplicating(mem):
    mem.store("NVDA is volatile", source="inferred")
    mem.store("NVDA is volatile", source="measured")
    facts = mem.load()
    assert len(facts) == 1, "repetition created a duplicate — volume would outvote evidence"
    assert facts[0].source == "measured", "stronger evidence did not upgrade the fact"


def test_a_weaker_repeat_does_not_downgrade_a_stronger_fact(mem):
    mem.store("user prefers defined risk", source="stated")
    mem.store("user prefers defined risk", source="inferred")
    assert mem.load()[0].source == "stated"


def test_recall_withholds_decayed_facts_rather_than_ranking_them_last(mem):
    """
    Bottom of the list is still in the context window, and once there a decayed
    measurement reads exactly like a fresh one.
    """
    f = mem.store("stale conclusion about NVDA", source="inferred", subject="NVDA")
    f.learned_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).isoformat()
    mem._write("memory", [f])
    assert mem.recall("NVDA") == []


def test_reflect_reports_contradictions_without_resolving_them(mem):
    mem.store("short strangle on NVDA is profitable", source="inferred", subject="NVDA")
    mem.store("short strangle on NVDA is not profitable", source="measured", subject="NVDA")
    r = mem.reflect()
    assert r["contradictions"], "opposite beliefs about the same subject went unflagged"
    assert "not profitable" in r["contradictions"][0]["prefer"], "the measured side should be preferred"
    assert len(mem.load()) == 2, "reflect deleted a fact — it should report, not adjudicate"


def test_forget_removes_matching_facts(mem):
    mem.store("temporary note about TSLA", source="inferred")
    assert mem.forget("TSLA") == 1
    assert mem.load() == []


def test_injection_states_source_and_confidence_for_every_fact(mem):
    mem.store("prefers 45 DTE", source="stated", which="user")
    mem.store("AAPL iron condors won 7 of 10 backtests", source="measured", subject="AAPL")
    block = mem.inject({"q": "AAPL condor"})
    assert "[measured" in block and "prefers 45 DTE" in block
    import re as _re
    assert _re.search(r"\[measured · \d\.\d\d\]", block), \
        f"confidence was not surfaced with the fact: {block!r}"


def test_extraction_learns_only_from_tool_results(mem):
    """
    A store that learns from its own prose drifts without ever recording a step
    that can be challenged. Only measurements get written.
    """
    facts = mem.extract("s1", [
        {"role": "assistant", "content": "I think NVDA looks great here"},
        {"role": "tool", "tool_name": "backtest.run", "content": "total_pl=-412"},
    ])
    assert len(facts) == 1
    assert facts[0].source == "measured" and "backtest.run" in facts[0].text


def test_an_empty_memory_says_so_plainly(mem):
    assert "Nothing has been learned yet" in mem.reflect()["nudge"]


def test_manager_runs_the_hermes_turn_lifecycle_and_extracts_measurements(mem):
    manager = MemoryManager(mem)
    assert manager.add_provider(BuiltinMemoryProvider(mem))
    manager.initialize_all("session-1", platform="test")
    manager.on_turn_start("session-1", "backtest NVDA")
    assert "Durable user" in manager.build_system_prompt()
    manager.sync_all("backtest NVDA", "done", session_id="session-1", messages=[
        {"role": "tool", "tool_name": "backtest.run", "content": "expectancy=0.12"},
    ])
    manager.shutdown()
    assert any("backtest.run" in fact.text for fact in mem.load())


def test_manager_rejects_a_second_external_provider(mem):
    class External:
        name = "one"
        is_available = lambda self: True
        initialize = lambda self, *args, **kwargs: None
        system_prompt_block = lambda self: ""
        prefetch = lambda self, *args, **kwargs: ""
        queue_prefetch = lambda self, *args, **kwargs: None
        sync_turn = lambda self, *args, **kwargs: None
        get_tool_schemas = lambda self: []
        handle_tool_call = lambda self, *args, **kwargs: "{}"
        shutdown = lambda self: None
        on_turn_start = lambda self, *args, **kwargs: None
        on_session_end = lambda self, *args, **kwargs: None
        on_session_switch = lambda self, *args, **kwargs: None
        on_pre_compress = lambda self, *args, **kwargs: ""
        on_memory_write = lambda self, *args, **kwargs: None

    manager = MemoryManager(mem)
    assert manager.add_provider(BuiltinMemoryProvider(mem))
    assert manager.add_provider(External())
    with pytest.raises(ValueError, match="one external"):
        manager.add_provider(External())
    manager.shutdown()


# --------------------------------------------------------------- skills ---

@pytest.fixture
def lib(tmp_path):
    return SkillLibrary(tmp_path)


def test_a_new_skill_starts_unproven_with_no_success_rate(lib):
    s = lib.create(name="Roll at 21 DTE", trigger="position reaches 21 DTE", body="Roll out.")
    assert s.status == "trial"
    assert s.success_rate is None, "an unused skill reported a rate it cannot have"
    assert "never been scored" in s.verdict()


def test_there_is_no_path_that_creates_an_already_active_skill(lib):
    assert lib.create(name="x", trigger="y", body="z", origin="user").status == "trial"


def test_a_small_lucky_sample_does_not_promote(lib):
    s = lib.create(name="Lucky", trigger="t", body="b")
    for _ in range(3):
        lib.record(s.id, True)
    got = lib.get(s.id)
    assert got.status == "trial", "3/3 promoted a skill — that is a coin landing heads"
    assert got.success_rate == 1.0
    assert "Too few trials" in got.verdict()


def test_enough_evidence_promotes(lib):
    s = lib.create(name="Good", trigger="t", body="b")
    for ok in [True] * 7 + [False]:
        lib.record(s.id, ok)
    got = lib.get(s.id)
    assert got.trials >= MIN_TRIALS
    assert got.status == "active" and got.lower_bound >= BASELINE


def test_a_losing_skill_is_demoted_and_the_demotion_is_recorded(lib):
    """
    The decisive test. Promotion-only is accumulation, not learning — an agent
    that cannot conclude its own skill is failing will keep applying it.
    """
    s = lib.create(name="Bad", trigger="t", body="b")
    for ok in [False] * 7 + [True]:
        lib.record(s.id, ok)
    got = lib.get(s.id)
    assert got.status == "demoted"
    assert "not beating doing nothing" in got.verdict()
    assert any("-> demoted" in h.get("event", "") for h in got.history)


def test_demoted_skills_are_excluded_from_matching(lib):
    s = lib.create(name="Bad condor rule", trigger="iron condor setups", body="b")
    for ok in [False] * 8:
        lib.record(s.id, ok)
    assert lib.match("iron condor setups") == [], "a measured-failing skill was still offered"


def test_revising_a_skill_resets_its_scoreboard(lib):
    """
    Carrying wins across a rewrite credits new text with results it never
    earned — and the temptation is strongest right after a bad run.
    """
    s = lib.create(name="Rule", trigger="t", body="old")
    for ok in [True] * 8:
        lib.record(s.id, ok)
    assert lib.get(s.id).status == "active"
    revised = lib.revise(s.id, body="new", reason="tightened the entry")
    assert revised.trials == 0 and revised.status == "trial" and revised.revision == 2
    assert any(h.get("event") == "revised" for h in revised.history)
    assert any(h.get("carried_successes") == 8 for h in revised.history), \
        "the discarded record should still be visible in history"


def test_wilson_prefers_the_better_evidenced_rate():
    assert wilson_lower_bound(3, 3) < wilson_lower_bound(60, 80)
    assert wilson_lower_bound(0, 0) == 0.0


def test_skills_survive_a_round_trip_through_markdown(lib):
    s = lib.create(name="Persisted", trigger="always", body="Do the thing.", subject="SPY")
    lib.record(s.id, True, note="worked")
    again = SkillLibrary(lib.home.parent).get(s.id)
    assert again is not None
    assert again.name == "Persisted" and again.subject == "SPY"
    assert again.trials == 1 and again.outcomes[0].note == "worked"


def test_the_report_states_how_much_is_unproven(lib):
    lib.create(name="A", trigger="t", body="b")
    lib.create(name="B", trigger="t", body="b")
    r = lib.report()
    assert r["total"] == 2
    assert "unproven" in r["note"]
    assert r["by_status"]["trial"] == 2
