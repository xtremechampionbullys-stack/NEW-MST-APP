"""
Memory, modelled on the Hermes Agent memory layer.

The documented design has two always-active files — `MEMORY.md` for what the
agent has learned and `USER.md` for who it is working with — plus a provider
interface with four phases: **context injection** before a turn, **prefetch**
of relevant memories, **synchronisation** of the turn afterwards, and
**extraction** of structured memories at session end. Writes to the built-in
files are mirrored to any external provider so the two cannot diverge.

Two things this adds on top, both because the domain demands it:

**Every fact carries provenance.** A trading memory that says "the wheel works
on AAPL" is worthless without knowing whether that came from the user saying so,
from a backtest, or from the agent's own inference. Same fact, three completely
different weights. So each line records how it was learned and, where it came
from a tool, which run produced it.

**Confidence decays.** A conclusion drawn from one 900-day backtest in March is
not the same claim in September, and a memory store that presents both at full
strength is quietly making the older one stronger than the evidence supports.
Facts sourced from measurement age; facts the user stated do not, because a
stated preference does not expire on a schedule.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"

# How a fact came to be known, strongest first. This ordering is load-bearing:
# it decides what survives a contradiction.
SOURCES = ("stated", "measured", "observed", "inferred")

# Half-life in days for measurement-derived confidence. A backtest conclusion is
# half as trustworthy about today's market three months on; a stated preference
# does not decay at all.
DECAY_HALF_LIFE = {"measured": 90.0, "observed": 45.0, "inferred": 30.0}
log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class Fact:
    text: str
    source: str = "inferred"
    learned_at: str = field(default_factory=lambda: _now().isoformat())
    run_id: str | None = None
    subject: str | None = None          # e.g. a ticker or a strategy id
    hits: int = 0                       # times recalled

    def age_days(self, now: dt.datetime | None = None) -> float:
        now = now or _now()
        try:
            then = dt.datetime.fromisoformat(self.learned_at)
        except ValueError:
            return 0.0
        if then.tzinfo is None:
            then = then.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - then).total_seconds() / 86400.0)

    def confidence(self, now: dt.datetime | None = None) -> float:
        """
        Present-day weight of this fact.

        Stated facts hold at 1.0 — the user told us, and that does not rot.
        Everything else decays on its own half-life, so a claim drawn from one
        old measurement stops outranking a fresh one purely by being written
        down first.
        """
        if self.source == "stated":
            return 1.0
        hl = DECAY_HALF_LIFE.get(self.source, 30.0)
        return round(0.5 ** (self.age_days(now) / hl), 4)

    def to_line(self) -> str:
        bits = [f"[{self.source}]"]
        if self.subject:
            bits.append(f"({self.subject})")
        bits.append(self.text)
        bits.append(f"<!-- learned:{self.learned_at}"
                    + (f" run:{self.run_id}" if self.run_id else "")
                    + f" hits:{self.hits} -->")
        return " ".join(bits)

    @classmethod
    def from_line(cls, line: str) -> "Fact | None":
        m = re.match(r"^\s*-?\s*\[(\w+)\]\s*(?:\(([^)]*)\)\s*)?(.*?)\s*(?:<!--(.*?)-->)?\s*$", line)
        if not m or not m.group(3):
            return None
        source, subject, text, meta = m.groups()
        learned, run_id, hits = _now().isoformat(), None, 0
        if meta:
            if (mm := re.search(r"learned:(\S+)", meta)):
                learned = mm.group(1)
            if (mm := re.search(r"run:(\S+)", meta)):
                run_id = mm.group(1)
            if (mm := re.search(r"hits:(\d+)", meta)):
                hits = int(mm.group(1))
        return cls(text=text.strip(), source=source if source in SOURCES else "inferred",
                   learned_at=learned, run_id=run_id, subject=subject, hits=hits)


class MemoryProvider(Protocol):
    """Hermes-compatible provider lifecycle.

    This is intentionally the same lifecycle shape as Hermes: a built-in
    provider is always present and at most one external provider may augment
    it.  The external store is never allowed to become a second authority.
    """

    name: str

    def is_available(self) -> bool: ...
    def initialize(self, session_id: str, **kwargs: Any) -> None: ...
    def system_prompt_block(self) -> str: ...
    def prefetch(self, query: str, *, session_id: str = "") -> str: ...
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None: ...
    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list[dict] | None = None) -> None: ...
    def get_tool_schemas(self) -> list[dict]: ...
    def handle_tool_call(self, tool_name: str, args: dict, **kwargs: Any) -> str: ...
    def shutdown(self) -> None: ...
    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None: ...
    def on_session_end(self, messages: list[dict]) -> None: ...
    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None: ...
    def on_pre_compress(self, messages: list[dict]) -> str: ...
    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: dict | None = None) -> None: ...


class MemoryStore:
    """
    The built-in store: MEMORY.md and USER.md, always active.

    `mirror` receives every write so an external provider cannot drift from the
    files. Upstream mirrors in the same direction and for the same reason: two
    stores that are each authoritative are one store that is wrong.
    """

    def __init__(self, home: str | Path, mirror: MemoryProvider | None = None):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.mirror = mirror

    # -- files ------------------------------------------------------------

    def _path(self, which: str) -> Path:
        return self.home / (USER_FILE if which == "user" else MEMORY_FILE)

    def load(self, which: str = "memory") -> list[Fact]:
        p = self._path(which)
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            f = Fact.from_line(line)
            if f:
                out.append(f)
        return out

    def _write(self, which: str, facts: list[Fact]) -> None:
        header = ("# User\n\nWho this agent works with. Stated facts only.\n\n"
                  if which == "user" else
                  "# Memory\n\nWhat this agent has learned. Each line records how it was\n"
                  "learned; confidence on measured facts decays with age.\n\n")
        body = "\n".join(f"- {f.to_line()}" for f in facts)
        self._path(which).write_text(header + body + "\n")

    # -- operations (the documented tool surface) -------------------------

    def store(self, text: str, *, source: str = "inferred", subject: str | None = None,
              run_id: str | None = None, which: str = "memory") -> Fact:
        """
        Add a fact, or strengthen an existing one.

        A repeat of something already known does not append a duplicate: it
        refreshes the timestamp and upgrades the source if the new evidence is
        stronger. Appending would let one fact repeated ten times outvote ten
        distinct facts purely by volume.
        """
        if source not in SOURCES:
            source = "inferred"
        facts = self.load(which)
        norm = _normalize(text)
        for f in facts:
            if _normalize(f.text) == norm:
                if SOURCES.index(source) < SOURCES.index(f.source):
                    f.source = source
                f.learned_at = _now().isoformat()
                f.run_id = run_id or f.run_id
                self._write(which, facts)
                self._mirror("update", f)
                return f
        fact = Fact(text=text, source=source, subject=subject, run_id=run_id)
        facts.append(fact)
        self._write(which, facts)
        self._mirror("store", fact)
        return fact

    def recall(self, query: str, *, limit: int = 8, which: str = "memory",
               min_confidence: float = 0.15) -> list[Fact]:
        """
        Retrieve by relevance × present-day confidence.

        Facts below `min_confidence` are withheld rather than returned last:
        surfacing a stale conclusion at the bottom of a list still puts it in
        front of the model, and a decayed measurement reads exactly like a
        current one once it is in the context window.
        """
        terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
        scored = []
        for f in self.load(which):
            conf = f.confidence()
            if conf < min_confidence:
                continue
            hay = f"{f.subject or ''} {f.text}".lower()
            overlap = sum(1 for t in terms if t in hay)
            if terms and not overlap:
                continue
            scored.append((overlap * conf, conf, f))
        scored.sort(key=lambda r: (-r[0], -r[1]))
        out = [f for _, _, f in scored[:limit]]
        for f in out:
            f.hits += 1
        if out:
            self._bump_hits(which, out)
        return out

    def forget(self, pattern: str, which: str = "memory") -> int:
        facts = self.load(which)
        keep = [f for f in facts if pattern.lower() not in f.text.lower()]
        removed = len(facts) - len(keep)
        if removed:
            self._write(which, keep)
            self._mirror("forget", Fact(text=pattern))
        return removed

    def reflect(self, which: str = "memory") -> dict:
        """
        Cross-memory reasoning: what is stale, thin, or contradicted.

        This is the "periodic nudge" in the documented design, and it reports
        problems rather than fixing them. Auto-pruning a contradiction picks a
        winner without evidence; showing the pair lets the next measurement
        settle it.
        """
        facts = self.load(which)
        now = _now()
        stale = [f for f in facts if f.confidence(now) < 0.3 and f.source != "stated"]
        unused = [f for f in facts if f.hits == 0 and f.age_days(now) > 14]

        # Contradictions: same subject, one asserting and one negating.
        contradictions = []
        by_subject: dict[str, list[Fact]] = {}
        for f in facts:
            by_subject.setdefault(f.subject or "", []).append(f)
        for subject, group in by_subject.items():
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if _contradicts(a.text, b.text):
                        contradictions.append({
                            "subject": subject,
                            "a": a.to_line(), "b": b.to_line(),
                            "prefer": (a if SOURCES.index(a.source) <= SOURCES.index(b.source) else b).text,
                        })

        return {
            "facts": len(facts),
            "by_source": {s: sum(1 for f in facts if f.source == s) for s in SOURCES},
            "stale": [f.to_line() for f in stale],
            "never_recalled": [f.to_line() for f in unused],
            "contradictions": contradictions,
            "nudge": _nudge(len(facts), stale, unused, contradictions),
        }

    # -- provider phases --------------------------------------------------

    def inject(self, ctx: dict, *, limit: int = 6) -> str:
        """Phase 1: the block that goes into the system prompt before a turn."""
        query = " ".join(str(v) for v in ctx.values() if isinstance(v, str))
        user = self.load("user")[:4]
        facts = self.recall(query, limit=limit)
        if not user and not facts:
            return ""
        lines = ["## Memory"]
        if user:
            lines += ["", "About the user:"] + [f"- {f.text}" for f in user]
        if facts:
            lines += ["", "Relevant, with how it was learned and current confidence:"]
            lines += [f"- [{f.source} · {f.confidence():.2f}] {f.text}" for f in facts]
        return "\n".join(lines)

    def extract(self, session_id: str, messages: list[dict]) -> list[Fact]:
        """
        Phase 4: structured facts at session end.

        Deliberately conservative. Only tool results are extracted, because a
        tool result is a measurement with a provenance, whereas a sentence in
        the transcript is whatever the model happened to say — and a memory
        store that learns from its own prose drifts away from reality without
        ever recording a step it can be challenged on.
        """
        out = []
        for m in messages:
            if m.get("role") != "tool" or not m.get("content"):
                continue
            tool = m.get("tool_name") or "tool"
            text = str(m["content"])[:400]
            out.append(self.store(f"{tool}: {text}", source="measured",
                                  subject=m.get("subject"), run_id=session_id))
        return out

    # -- internals --------------------------------------------------------

    def _bump_hits(self, which: str, recalled: list[Fact]) -> None:
        seen = {_normalize(f.text) for f in recalled}
        facts = self.load(which)
        for f in facts:
            if _normalize(f.text) in seen:
                f.hits += 1
        self._write(which, facts)

    def _mirror(self, op: str, fact: Fact) -> None:
        if not self.mirror:
            return
        try:
            getattr(self.mirror, "on_write", lambda *_: None)(op, fact)
        except Exception:                                       # noqa: BLE001
            # A failing external provider must not take the built-in files with
            # it: the files are the source of truth, the provider is a mirror.
            pass


class BuiltinMemoryProvider:
    """Adapter making MarketShark's durable files a Hermes built-in provider."""

    name = "builtin"

    def __init__(self, store: MemoryStore):
        self.store = store

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        del session_id, kwargs

    def system_prompt_block(self) -> str:
        return ("Durable user and trading memory is fallible. Preserve its source, "
                "confidence, and evidence run; never turn it into an execution instruction.")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        del session_id
        return self.store.inject({"message": query})

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Local markdown recall is synchronous and cheap. The hook exists so
        # an external vector/graph provider can prewarm the following turn.
        del query, session_id

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list[dict] | None = None) -> None:
        del user_content, assistant_content
        if messages:
            self.store.extract(session_id, messages)

    def get_tool_schemas(self) -> list[dict]:
        return []

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs: Any) -> str:
        raise KeyError(f"builtin memory has no tool named {tool_name}")

    def shutdown(self) -> None:
        return None

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        del turn_number, message, kwargs

    def on_session_end(self, messages: list[dict]) -> None:
        # Extract again on a real boundary. store() de-duplicates this safely.
        self.store.extract("session-end", messages)

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        del new_session_id, kwargs

    def on_pre_compress(self, messages: list[dict]) -> str:
        measured = [m.get("content", "")[:320] for m in messages if m.get("role") == "tool"]
        return "\n".join(measured[:8])

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: dict | None = None) -> None:
        del action, target, content, metadata


class MemoryManager:
    """Hermes-style memory orchestration for MarketShark.

    The manager has an always-on built-in provider plus *one* optional external
    provider. Retrieval is bounded and failures are isolated. Turn writes are
    serialised off the response path, preserving order without allowing a slow
    remote memory backend to hold a trading workspace hostage.
    """

    def __init__(self, store: MemoryStore, *, external_prefetch_timeout: float = 1.5):
        self.store = store
        self.providers: list[MemoryProvider] = []
        self._tools: dict[str, MemoryProvider] = {}
        self._has_external = False
        self._turns: dict[str, int] = {}
        self._initialized_sessions: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marketshark-memory")
        self._futures: set[Future] = set()
        self._lock = threading.RLock()
        self.external_prefetch_timeout = external_prefetch_timeout
        # Built-in markdown files remain authoritative; manager fans each
        # mutation to the optional external provider through on_write().
        self.store.mirror = self

    def add_provider(self, provider: MemoryProvider) -> bool:
        if not provider.is_available():
            return False
        if provider.name != "builtin":
            if self._has_external:
                raise ValueError("Only one external memory provider may be active")
            self._has_external = True
        self.providers.append(provider)
        for schema in provider.get_tool_schemas():
            name = str(schema.get("name", "")).strip()
            if name and name not in self._tools:
                self._tools[name] = provider
        return True

    def initialize_all(self, session_id: str, **kwargs: Any) -> None:
        if session_id in self._initialized_sessions:
            return
        self._initialized_sessions.add(session_id)
        for provider in self.providers:
            try:
                provider.initialize(session_id, **kwargs)
            except Exception:
                log.exception("memory provider %s failed to initialize", provider.name)

    def build_system_prompt(self) -> str:
        blocks = []
        for provider in self.providers:
            try:
                block = provider.system_prompt_block()
                if block:
                    blocks.append(block.strip())
            except Exception:
                log.exception("memory provider %s failed system-prompt assembly", provider.name)
        return "\n\n".join(blocks)

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        blocks = []
        for provider in self.providers:
            try:
                # The bundled provider is local; external providers are given a
                # short deadline so a recall outage cannot stall a market turn.
                if provider.name == "builtin":
                    block = provider.prefetch(query, session_id=session_id)
                else:
                    future = self._executor.submit(provider.prefetch, query, session_id=session_id)
                    block = future.result(timeout=self.external_prefetch_timeout)
                if block:
                    blocks.append(str(block).strip())
            except Exception:
                log.warning("memory provider %s prefetch failed", provider.name, exc_info=True)
        return "\n\n".join(blocks)

    def _submit(self, fn) -> None:
        future = self._executor.submit(fn)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(lambda done: self._futures.discard(done))

    def queue_prefetch_all(self, query: str, *, session_id: str = "") -> None:
        def _run() -> None:
            for provider in self.providers:
                try:
                    provider.queue_prefetch(query, session_id=session_id)
                except Exception:
                    log.warning("memory provider %s queued prefetch failed", provider.name, exc_info=True)
        self._submit(_run)

    def sync_all(self, user_content: str, assistant_content: str, *, session_id: str,
                 messages: list[dict] | None = None) -> None:
        def _run() -> None:
            for provider in self.providers:
                try:
                    provider.sync_turn(user_content, assistant_content, session_id=session_id, messages=messages)
                except Exception:
                    log.warning("memory provider %s sync failed", provider.name, exc_info=True)
        self._submit(_run)

    def on_turn_start(self, session_id: str, message: str, **kwargs: Any) -> None:
        turn = self._turns.get(session_id, 0) + 1
        self._turns[session_id] = turn
        for provider in self.providers:
            try:
                provider.on_turn_start(turn, message, **kwargs)
            except Exception:
                log.warning("memory provider %s turn hook failed", provider.name, exc_info=True)

    def on_session_end(self, session_id: str, messages: list[dict]) -> None:
        def _run() -> None:
            for provider in self.providers:
                try:
                    provider.on_session_end(messages)
                except Exception:
                    log.warning("memory provider %s session-end hook failed", provider.name, exc_info=True)
        self._turns.pop(session_id, None)
        self._initialized_sessions.discard(session_id)
        self._submit(_run)

    def on_session_switch(self, old_session_id: str, new_session_id: str, **kwargs: Any) -> None:
        for provider in self.providers:
            try:
                provider.on_session_switch(new_session_id, parent_session_id=old_session_id, **kwargs)
            except Exception:
                log.warning("memory provider %s session-switch hook failed", provider.name, exc_info=True)

    def on_pre_compress(self, messages: list[dict]) -> str:
        blocks = []
        for provider in self.providers:
            try:
                block = provider.on_pre_compress(messages)
                if block:
                    blocks.append(block)
            except Exception:
                log.warning("memory provider %s pre-compress hook failed", provider.name, exc_info=True)
        return "\n\n".join(blocks)

    def on_write(self, action: str, fact: Fact) -> None:
        """Called by MemoryStore after every authoritative file mutation."""
        target = "user" if fact.source == "stated" else "memory"
        for provider in self.providers:
            if provider.name == "builtin":
                continue
            try:
                provider.on_memory_write(action, target, fact.text, {
                    "source": fact.source, "subject": fact.subject,
                    "run_id": fact.run_id, "learned_at": fact.learned_at,
                })
            except Exception:
                log.warning("memory provider %s mirror failed", provider.name, exc_info=True)

    def get_tool_schemas(self) -> list[dict]:
        return [schema for provider in self.providers for schema in provider.get_tool_schemas()]

    def handle_tool_call(self, tool_name: str, args: dict) -> str:
        provider = self._tools.get(tool_name)
        if provider is None:
            raise KeyError(f"unknown memory tool: {tool_name}")
        return provider.handle_tool_call(tool_name, args)

    def shutdown(self, timeout: float = 3.0) -> None:
        """Bounded FIFO drain mirroring Hermes' durable shutdown semantics."""
        with self._lock:
            pending = list(self._futures)
        for future in pending:
            try:
                future.result(timeout=timeout)
            except Exception:
                pass
        for provider in self.providers:
            try:
                provider.shutdown()
            except Exception:
                log.warning("memory provider %s shutdown failed", provider.name, exc_info=True)
        self._executor.shutdown(wait=False, cancel_futures=False)


def _normalize(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


_NEGATORS = ("no ", "not ", "never ", "n't ", "fails", "failed", "without", "unprofitable")


def _contradicts(a: str, b: str) -> bool:
    """
    Cheap negation check over a shared subject.

    Not a semantic entailment model, and it does not pretend to be: it flags
    pairs for a human or a later measurement to settle. False positives cost a
    line in a report; false negatives leave two opposite beliefs both live.
    """
    na, nb = _normalize(a), _normalize(b)
    if na == nb:
        return False
    shared = set(na.split()) & set(nb.split())
    if len(shared) < 3:
        return False
    neg_a = any(w in f" {na} " for w in _NEGATORS)
    neg_b = any(w in f" {nb} " for w in _NEGATORS)
    return neg_a != neg_b


def _nudge(total: int, stale, unused, contradictions) -> str:
    if contradictions:
        return (f"{len(contradictions)} contradiction(s) in memory. They are shown "
                f"rather than resolved — the stronger source is suggested, but a "
                f"measurement should settle it, not a tiebreak rule.")
    if stale:
        return (f"{len(stale)} fact(s) have decayed below 0.3 confidence. They were "
                f"measured once and have not been re-measured since; re-run the "
                f"backtest or forget them.")
    if unused:
        return (f"{len(unused)} fact(s) have never been recalled in over two weeks. "
                f"Memory that is never retrieved is cost without benefit.")
    if total == 0:
        return "Memory is empty. Nothing has been learned yet — that is a true statement, not a fault."
    return f"{total} facts, none stale or contradicted."
