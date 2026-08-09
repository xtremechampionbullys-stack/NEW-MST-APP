"""
Session state, modelled on the Hermes Agent state layer.

Reproduced from the published implementation (NousResearch/hermes-agent,
`hermes_state.py`) rather than invented: SQLite-backed sessions and messages,
WAL mode for many readers and one writer, two FTS5 virtual tables — a unicode61
tokenizer for ordinary word search and a **trigram** tokenizer so substring
queries work in scripts that do not delimit words — kept in sync by triggers,
and writes wrapped in BEGIN IMMEDIATE with jittered retry.

Where the published source is explicit, this follows it. Where it is not — the
exact retry constants, the compression-child marker strings — this implements
the documented behaviour and says so rather than inventing a number and
presenting it as the original.

Why the trigram table earns its cost: with only the standard tokenizer, a
recall for "iron condor" finds nothing in a message that says "iron_condor",
and a user searching their own history has no way to know the search silently
under-reported rather than genuinely found nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_WRITE_MAX_RETRIES = 15
_WRITE_BACKOFF = (0.020, 0.150)     # seconds; jittered to break convoys
_CHECKPOINT_EVERY = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    user_id TEXT,
    model TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id, started_at);
CREATE INDEX IF NOT EXISTS sessions_parent ON sessions (parent_session_id);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    token_count INTEGER,
    tool_name TEXT,
    tool_calls TEXT,
    reasoning TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id, timestamp);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The adaptive strategy harness. These records intentionally live beside the
-- conversation state: a strategy conclusion without the session/evidence that
-- produced it is just a claim. No status here can authorize an order.
CREATE TABLE IF NOT EXISTS strategy_journeys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    symbol TEXT,
    thesis TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    source_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS strategy_journeys_symbol ON strategy_journeys (symbol, updated_at);

CREATE TABLE IF NOT EXISTS strategy_experiments (
    id TEXT PRIMARY KEY,
    journey_id TEXT NOT NULL REFERENCES strategy_journeys(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    template_id TEXT,
    hypothesis TEXT,
    parameters TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'testing',
    approval_required INTEGER NOT NULL DEFAULT 1,
    decision TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_experiments_journey ON strategy_experiments (journey_id, updated_at);

CREATE TABLE IF NOT EXISTS strategy_evidence (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES strategy_experiments(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    summary TEXT NOT NULL,
    metrics TEXT NOT NULL DEFAULT '{}',
    caveat TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS strategy_evidence_experiment ON strategy_evidence (experiment_id, created_at);

CREATE TABLE IF NOT EXISTS daily_briefs (
    id TEXT PRIMARY KEY,
    brief_date TEXT NOT NULL,
    symbol TEXT,
    content TEXT NOT NULL,
    inputs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS daily_briefs_date ON daily_briefs (brief_date, created_at);

-- Word search.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, message_id UNINDEXED, tokenize='unicode61');
-- Substring search. Overlapping 3-byte sequences, so "condor" is found inside
-- "iron_condor" and CJK/Thai text is searchable at all.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram
    USING fts5(content, message_id UNINDEXED, tokenize='trigram');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(content, message_id) VALUES (new.content, new.id);
    INSERT INTO messages_fts_trigram(content, message_id) VALUES (new.content, new.id);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.id;
    DELETE FROM messages_fts_trigram WHERE message_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.id;
    DELETE FROM messages_fts_trigram WHERE message_id = old.id;
    INSERT INTO messages_fts(content, message_id) VALUES (new.content, new.id);
    INSERT INTO messages_fts_trigram(content, message_id) VALUES (new.content, new.id);
END;
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@dataclass
class Message:
    role: str
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(default_factory=_now)
    tool_name: str | None = None
    tool_calls: list | None = None
    reasoning: str | None = None
    token_count: int | None = None

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in
             ("id", "role", "content", "timestamp", "tool_name", "reasoning", "token_count")}
        d["tool_calls"] = self.tool_calls or []
        return d


class SessionDB:
    """
    SQLite session storage with FTS5 search.

    Thread-safe for the gateway pattern the upstream implementation targets:
    many reader threads, a single writer serialised through WAL.
    """

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.RLock()
        self._writes = 0
        self._wal = True
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A shared connection for :memory: — otherwise every connect() gets a
        # fresh, empty database and the store silently loses everything.
        self._shared = sqlite3.connect(self.path, check_same_thread=False) \
            if self.path == ":memory:" else None
        self._init_schema()

    # -- connection -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            conn = self._shared
        else:
            conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self._wal and self._shared is None:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                # NFS and SMB reject WAL with a locking-protocol error. Degrade
                # to the pre-WAL journal rather than failing to open at all:
                # losing concurrency is recoverable, losing the session is not.
                if "locking protocol" not in str(exc).lower():
                    raise
                self._wal = False
                conn.execute("PRAGMA journal_mode=DELETE")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA)
        conn.commit()
        if self._shared is None:
            conn.close()

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """
        Run a write in BEGIN IMMEDIATE with jittered retry.

        The jitter is the point. Fixed backoff makes every blocked writer wake
        at the same instant and collide again — a convoy that turns transient
        contention into sustained failure.
        """
        last: Exception | None = None
        for attempt in range(_WRITE_MAX_RETRIES):
            conn = self._connect()
            try:
                with self._lock:
                    conn.execute("BEGIN IMMEDIATE")
                    out = fn(conn)
                    conn.commit()
                    self._writes += 1
                    if self._writes % _CHECKPOINT_EVERY == 0 and self._wal and self._shared is None:
                        try:
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except sqlite3.OperationalError:
                            pass
                    return out
            except sqlite3.OperationalError as exc:
                last = exc
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                time.sleep(random.uniform(*_WRITE_BACKOFF))
            finally:
                if self._shared is None:
                    conn.close()
        raise RuntimeError(f"write failed after {_WRITE_MAX_RETRIES} retries: {last}")

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            if self._shared is None:
                conn.close()

    # -- sessions ---------------------------------------------------------

    def create_session(self, *, source: str = "api", user_id: str | None = None,
                       model: str | None = None, system_prompt: str | None = None,
                       parent_session_id: str | None = None,
                       title: str | None = None, meta: dict | None = None) -> str:
        sid = uuid.uuid4().hex

        def _w(conn):
            conn.execute(
                "INSERT INTO sessions (id, source, user_id, model, system_prompt, "
                "parent_session_id, started_at, title, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, source, user_id, model, system_prompt, parent_session_id,
                 _now(), title, json.dumps(meta or {})))
            return sid

        return self._execute_write(_w)

    def end_session(self, session_id: str, reason: str = "closed") -> None:
        def _w(conn):
            conn.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                         (_now(), reason, session_id))
        self._execute_write(_w)

    def get_session(self, session_id: str) -> dict | None:
        rows = self._query("SELECT * FROM sessions WHERE id=?", (session_id,))
        return dict(rows[0]) if rows else None

    def list_sessions(self, *, user_id: str | None = None, limit: int = 50) -> list[dict]:
        if user_id:
            rows = self._query(
                "SELECT * FROM sessions WHERE user_id=? AND archived=0 "
                "ORDER BY started_at DESC LIMIT ?", (user_id, limit))
        else:
            rows = self._query(
                "SELECT * FROM sessions WHERE archived=0 ORDER BY started_at DESC LIMIT ?",
                (limit,))
        return [dict(r) for r in rows]

    # -- messages ---------------------------------------------------------

    def add_message(self, session_id: str, msg: Message) -> str:
        def _w(conn):
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, timestamp, "
                "token_count, tool_name, tool_calls, reasoning) VALUES (?,?,?,?,?,?,?,?,?)",
                (msg.id, session_id, msg.role, msg.content, msg.timestamp,
                 msg.token_count, msg.tool_name,
                 json.dumps(msg.tool_calls) if msg.tool_calls else None,
                 msg.reasoning))
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id=?",
                (session_id,))
            return msg.id
        return self._execute_write(_w)

    def messages(self, session_id: str, limit: int = 500) -> list[dict]:
        rows = self._query(
            "SELECT * FROM messages WHERE session_id=? AND active=1 "
            "ORDER BY timestamp LIMIT ?", (session_id, limit))
        return [dict(r) for r in rows]

    # -- search -----------------------------------------------------------

    def search(self, query: str, *, limit: int = 20,
               session_id: str | None = None) -> list[dict]:
        """
        Cross-session recall over both FTS indexes.

        Word matches rank above substring matches — an exact term hit is a
        stronger signal than a fragment inside a longer token — but the
        substring pass runs regardless, because it is the only one that finds
        "condor" inside "iron_condor" or matches text in a script with no
        spaces. Reporting only the word pass would under-report silently, which
        is the failure a user cannot detect from the results.
        """
        q = (query or "").strip()
        if not q:
            return []
        safe = '"' + q.replace('"', '""') + '"'

        hits: dict[str, dict] = {}
        for table, kind in (("messages_fts", "word"), ("messages_fts_trigram", "substring")):
            sql = (f"SELECT m.*, bm25({table}) AS score FROM {table} "
                   f"JOIN messages m ON m.id = {table}.message_id "
                   f"WHERE {table} MATCH ? ")
            args: tuple = (safe,)
            if session_id:
                sql += "AND m.session_id = ? "
                args = (safe, session_id)
            sql += "ORDER BY score LIMIT ?"
            args = args + (limit,)
            try:
                rows = self._query(sql, args)
            except sqlite3.OperationalError:
                # A trigram index rejects queries shorter than 3 characters.
                continue
            for r in rows:
                d = hits.setdefault(r["id"], dict(r))
                d.setdefault("match", kind)
        out = list(hits.values())
        out.sort(key=lambda d: (d.get("match") != "word", d.get("score", 0)))
        return out[:limit]

    # -- kv ---------------------------------------------------------------

    def put_meta(self, key: str, value: Any) -> None:
        def _w(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), _now()))
        self._execute_write(_w)

    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM state_meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def stats(self) -> dict:
        s = self._query("SELECT COUNT(*) c FROM sessions")[0]["c"]
        m = self._query("SELECT COUNT(*) c FROM messages")[0]["c"]
        return {"sessions": s, "messages": m, "path": self.path,
                "journal_mode": "wal" if self._wal else "delete"}

    # -- adaptive strategy harness --------------------------------------

    @staticmethod
    def _decode_json(row: dict, *fields: str) -> dict:
        out = dict(row)
        for field_name in fields:
            value = out.get(field_name)
            if isinstance(value, str):
                try:
                    out[field_name] = json.loads(value)
                except json.JSONDecodeError:
                    out[field_name] = {}
        return out

    def create_journey(self, *, name: str, symbol: str | None = None,
                       thesis: str | None = None, source_session_id: str | None = None,
                       meta: dict | None = None) -> dict:
        journey_id = uuid.uuid4().hex
        now = _now()

        def _w(conn):
            conn.execute(
                "INSERT INTO strategy_journeys "
                "(id,name,symbol,thesis,status,source_session_id,created_at,updated_at,meta) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (journey_id, name, symbol, thesis, "active", source_session_id,
                 now, now, json.dumps(meta or {})),
            )
            return journey_id
        self._execute_write(_w)
        return self.get_journey(journey_id) or {}

    def get_journey(self, journey_id: str) -> dict | None:
        rows = self._query("SELECT * FROM strategy_journeys WHERE id=?", (journey_id,))
        return self._decode_json(dict(rows[0]), "meta") if rows else None

    def find_active_journey(self, *, symbol: str | None, template_id: str | None = None) -> dict | None:
        # A fresh user session can continue the same named investigation; the
        # template is kept in metadata to avoid adding arbitrary columns for
        # every new strategy type.
        if not symbol:
            return None
        rows = self._query(
            "SELECT * FROM strategy_journeys WHERE symbol=? AND status='active' "
            "ORDER BY updated_at DESC LIMIT 12", (symbol,))
        for row in rows:
            item = self._decode_json(dict(row), "meta")
            if not template_id or item.get("meta", {}).get("template_id") == template_id:
                return item
        return None

    def list_journeys(self, *, symbol: str | None = None, limit: int = 20) -> list[dict]:
        if symbol:
            rows = self._query(
                "SELECT * FROM strategy_journeys WHERE symbol=? ORDER BY updated_at DESC LIMIT ?",
                (symbol, limit))
        else:
            rows = self._query("SELECT * FROM strategy_journeys ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._decode_json(dict(row), "meta") for row in rows]

    def create_experiment(self, *, journey_id: str, kind: str,
                          template_id: str | None = None, hypothesis: str | None = None,
                          parameters: dict | None = None) -> dict:
        experiment_id = uuid.uuid4().hex
        now = _now()

        def _w(conn):
            conn.execute(
                "INSERT INTO strategy_experiments "
                "(id,journey_id,kind,template_id,hypothesis,parameters,status,approval_required,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (experiment_id, journey_id, kind, template_id, hypothesis,
                 json.dumps(parameters or {}), "testing", 1, now, now),
            )
            conn.execute("UPDATE strategy_journeys SET updated_at=? WHERE id=?", (now, journey_id))
            return experiment_id
        self._execute_write(_w)
        return self.get_experiment(experiment_id) or {}

    def get_experiment(self, experiment_id: str) -> dict | None:
        rows = self._query("SELECT * FROM strategy_experiments WHERE id=?", (experiment_id,))
        if not rows:
            return None
        item = self._decode_json(dict(rows[0]), "parameters")
        evidence = self._query(
            "SELECT * FROM strategy_evidence WHERE experiment_id=? ORDER BY created_at", (experiment_id,))
        item["evidence"] = [self._decode_json(dict(row), "metrics") for row in evidence]
        return item

    def add_evidence(self, *, experiment_id: str, source: str, mode: str,
                     summary: str, metrics: dict | None = None,
                     caveat: str | None = None) -> dict:
        evidence_id = uuid.uuid4().hex
        now = _now()

        def _w(conn):
            conn.execute(
                "INSERT INTO strategy_evidence "
                "(id,experiment_id,source,mode,summary,metrics,caveat,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (evidence_id, experiment_id, source, mode, summary,
                 json.dumps(metrics or {}), caveat, now),
            )
            conn.execute(
                "UPDATE strategy_experiments SET updated_at=? WHERE id=?", (now, experiment_id))
            conn.execute(
                "UPDATE strategy_journeys SET updated_at=? WHERE id=(SELECT journey_id FROM strategy_experiments WHERE id=?)",
                (now, experiment_id))
            return evidence_id
        self._execute_write(_w)
        rows = self._query("SELECT * FROM strategy_evidence WHERE id=?", (evidence_id,))
        return self._decode_json(dict(rows[0]), "metrics") if rows else {}

    def decide_experiment(self, experiment_id: str, *, decision: str, note: str | None = None) -> dict:
        if decision not in {"paper_ready", "rejected", "hold"}:
            raise ValueError("decision must be paper_ready, rejected, or hold")
        now = _now()

        def _w(conn):
            conn.execute(
                "UPDATE strategy_experiments SET status=?, decision=?, decision_note=?, updated_at=? WHERE id=?",
                (decision, decision, note, now, experiment_id),
            )
        self._execute_write(_w)
        return self.get_experiment(experiment_id) or {}

    def list_experiments(self, *, journey_id: str | None = None, limit: int = 30) -> list[dict]:
        if journey_id:
            rows = self._query(
                "SELECT e.*, COUNT(v.id) AS evidence_count FROM strategy_experiments e "
                "LEFT JOIN strategy_evidence v ON v.experiment_id=e.id WHERE e.journey_id=? "
                "GROUP BY e.id ORDER BY e.updated_at DESC LIMIT ?", (journey_id, limit))
        else:
            rows = self._query(
                "SELECT e.*, COUNT(v.id) AS evidence_count FROM strategy_experiments e "
                "LEFT JOIN strategy_evidence v ON v.experiment_id=e.id "
                "GROUP BY e.id ORDER BY e.updated_at DESC LIMIT ?", (limit,))
        return [self._decode_json(dict(row), "parameters") for row in rows]

    def create_daily_brief(self, *, brief_date: str, content: str,
                           symbol: str | None = None, inputs: dict | None = None) -> dict:
        brief_id = uuid.uuid4().hex
        now = _now()
        def _w(conn):
            conn.execute(
                "INSERT INTO daily_briefs (id,brief_date,symbol,content,inputs,created_at) VALUES (?,?,?,?,?,?)",
                (brief_id, brief_date, symbol, content, json.dumps(inputs or {}), now))
        self._execute_write(_w)
        rows = self._query("SELECT * FROM daily_briefs WHERE id=?", (brief_id,))
        return self._decode_json(dict(rows[0]), "inputs") if rows else {}

    def harness_overview(self, *, limit: int = 12) -> dict:
        journeys = self.list_journeys(limit=limit)
        experiments = self.list_experiments(limit=limit)
        counts = self._query(
            "SELECT "
            "(SELECT COUNT(*) FROM strategy_journeys) AS journeys, "
            "(SELECT COUNT(*) FROM strategy_experiments) AS experiments, "
            "(SELECT COUNT(*) FROM strategy_evidence) AS evidence, "
            "(SELECT COUNT(*) FROM strategy_experiments WHERE status='paper_ready') AS paper_ready")
        return {"counts": dict(counts[0]), "journeys": journeys, "experiments": experiments,
                "execution_policy": "research and paper workflow only; no experiment can authorize a live order"}
