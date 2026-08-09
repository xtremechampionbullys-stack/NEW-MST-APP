"""
Skills: created from experience, improved during use — and scored honestly.

The documented Hermes behaviour is autonomous skill creation from experience
plus self-improvement while the skill is used. That is straightforward to
implement and dangerous to implement naively, because the obvious version is a
machine for manufacturing confidence: the agent writes down what worked once,
calls it a skill, uses it, and every subsequent use is read as confirmation.

So a skill here is a hypothesis with a scoreboard attached:

* Every invocation records an outcome. A skill with no recorded outcomes has no
  success rate — it reports `None`, not zero and not 100%.
* Promotion past `trial` requires enough trials to distinguish the skill from
  luck. The bar is a Wilson lower bound, not a raw ratio, so three wins out of
  three does not outrank sixty wins out of eighty.
* A skill whose lower bound falls under the baseline is **demoted**, and the
  demotion is in the record. Self-improvement that can only revise upward is
  not learning, it is accumulation.

That last property is the one that makes the rest worth anything. An agent that
cannot conclude "this skill I wrote is not working" will keep applying it.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Trials needed before a skill can leave `trial`. Below this, a success rate is
# noise: two-for-two is a coin landing heads twice.
MIN_TRIALS = 8
# The rate a skill must beat to be worth invoking at all. Doing nothing is the
# alternative, and doing nothing has a cost of zero.
BASELINE = 0.5
STATUSES = ("trial", "active", "demoted", "retired")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def wilson_lower_bound(successes: int, trials: int, z: float = 1.96) -> float:
    """
    Lower bound of the 95% confidence interval on a success rate.

    This is what stops a small lucky sample from promoting itself. 3/3 gives
    0.44; 60/80 gives 0.64 — so the better-evidenced skill wins even though its
    raw rate is lower. Using the raw ratio instead would rank every brand-new
    skill above every proven one, permanently.
    """
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
    return max(0.0, (centre - margin) / denom)


@dataclass
class Outcome:
    ok: bool
    at: str = field(default_factory=_now)
    note: str = ""
    run_id: str | None = None


@dataclass
class Skill:
    name: str
    trigger: str                     # when this applies, in plain words
    body: str                        # what to do
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    subject: str | None = None       # ticker, template, or None for general
    status: str = "trial"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    revision: int = 1
    outcomes: list[Outcome] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    origin: str = "agent"            # agent | user | imported

    # -- scoring ----------------------------------------------------------

    @property
    def trials(self) -> int:
        return len(self.outcomes)

    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def success_rate(self) -> float | None:
        """None until there is anything to measure. Never a default."""
        return (self.successes / self.trials) if self.trials else None

    @property
    def lower_bound(self) -> float:
        return wilson_lower_bound(self.successes, self.trials)

    def verdict(self) -> str:
        if self.trials == 0:
            return ("No recorded outcomes. This skill has never been scored — it is "
                    "a written-down hunch, not a demonstrated one.")
        if self.trials < MIN_TRIALS:
            return (f"{self.successes}/{self.trials} so far. Too few trials to tell it "
                    f"from luck; needs {MIN_TRIALS - self.trials} more before the rate "
                    f"means anything.")
        if self.lower_bound < BASELINE:
            return (f"{self.successes}/{self.trials} = {self.success_rate:.0%}, but the "
                    f"95% lower bound is {self.lower_bound:.0%}, under the {BASELINE:.0%} "
                    f"baseline. On this evidence the skill is not beating doing nothing.")
        return (f"{self.successes}/{self.trials} = {self.success_rate:.0%}, lower bound "
                f"{self.lower_bound:.0%}. Beating baseline on the evidence so far.")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trials"] = self.trials
        d["successes"] = self.successes
        d["success_rate"] = self.success_rate
        d["lower_bound"] = round(self.lower_bound, 4)
        d["verdict"] = self.verdict()
        return d

    # -- markdown form ----------------------------------------------------

    def to_markdown(self) -> str:
        meta = {"id": self.id, "status": self.status, "revision": self.revision,
                "subject": self.subject, "origin": self.origin,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "outcomes": [asdict(o) for o in self.outcomes],
                "history": self.history}
        return (f"---\n{json.dumps(meta, indent=2)}\n---\n\n"
                f"# {self.name}\n\n"
                f"**When:** {self.trigger}\n\n"
                f"{self.body}\n\n"
                f"---\n_{self.verdict()}_\n")

    @classmethod
    def from_markdown(cls, text: str) -> "Skill | None":
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            return None
        meta = json.loads(m.group(1))
        rest = m.group(2)
        name = (re.search(r"^#\s+(.+)$", rest, re.M) or [None, "Untitled"])[1]
        trigger = (re.search(r"\*\*When:\*\*\s*(.+)$", rest, re.M) or [None, ""])[1]
        body = re.sub(r"^#.*$|^\*\*When:\*\*.*$|^---$|^_.*_$", "", rest, flags=re.M).strip()
        return cls(
            name=name.strip(), trigger=trigger.strip(), body=body,
            id=meta.get("id", uuid.uuid4().hex[:12]), subject=meta.get("subject"),
            status=meta.get("status", "trial"), revision=meta.get("revision", 1),
            created_at=meta.get("created_at", _now()), updated_at=meta.get("updated_at", _now()),
            origin=meta.get("origin", "agent"),
            outcomes=[Outcome(**o) for o in meta.get("outcomes", [])],
            history=meta.get("history", []),
        )


class SkillLibrary:
    """Skills on disk, one markdown file each, with their scoreboards inline."""

    def __init__(self, home: str | Path):
        self.home = Path(home) / "skills"
        self.home.mkdir(parents=True, exist_ok=True)

    def _path(self, skill: Skill) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", skill.name.lower()).strip("-")[:48]
        return self.home / f"{slug}-{skill.id}.md"

    def all(self) -> list[Skill]:
        out = []
        for p in sorted(self.home.glob("*.md")):
            s = Skill.from_markdown(p.read_text())
            if s:
                out.append(s)
        return out

    def get(self, skill_id: str) -> Skill | None:
        return next((s for s in self.all() if s.id == skill_id), None)

    def save(self, skill: Skill) -> Skill:
        skill.updated_at = _now()
        for p in self.home.glob(f"*-{skill.id}.md"):
            p.unlink()
        self._path(skill).write_text(skill.to_markdown())
        return skill

    # -- creation ---------------------------------------------------------

    def create(self, *, name: str, trigger: str, body: str,
               subject: str | None = None, origin: str = "agent") -> Skill:
        """
        Write a new skill.

        It starts in `trial` with no outcomes, so nothing that reads the library
        can mistake a fresh hunch for a demonstrated one. There is no path that
        creates a skill already marked active.
        """
        skill = Skill(name=name, trigger=trigger, body=body, subject=subject,
                      origin=origin)
        skill.history.append({"at": _now(), "event": "created", "revision": 1})
        return self.save(skill)

    # -- use and improvement ----------------------------------------------

    def record(self, skill_id: str, ok: bool, *, note: str = "",
               run_id: str | None = None) -> Skill | None:
        """
        Record one outcome and re-evaluate status.

        Promotion and demotion run on the same rule in both directions. A
        library that can only promote is a library that only ever grows more
        confident, which is the failure this whole module exists to avoid.
        """
        skill = self.get(skill_id)
        if not skill:
            return None
        skill.outcomes.append(Outcome(ok=ok, note=note, run_id=run_id))

        before = skill.status
        if skill.trials >= MIN_TRIALS:
            skill.status = "active" if skill.lower_bound >= BASELINE else "demoted"
        if skill.status != before:
            skill.history.append({
                "at": _now(), "event": f"{before} -> {skill.status}",
                "trials": skill.trials, "successes": skill.successes,
                "lower_bound": round(skill.lower_bound, 4),
            })
        return self.save(skill)

    def revise(self, skill_id: str, *, body: str | None = None,
               trigger: str | None = None, reason: str = "") -> Skill | None:
        """
        Edit a skill in place, resetting its scoreboard.

        The reset is the important part and it is not optional. Carrying results
        from the old text over to the new one credits the revision with wins it
        never earned — and revising after a bad run is exactly when that
        temptation is strongest.
        """
        skill = self.get(skill_id)
        if not skill:
            return None
        skill.history.append({
            "at": _now(), "event": "revised", "reason": reason,
            "from_revision": skill.revision,
            "carried_trials": skill.trials, "carried_successes": skill.successes,
            "note": "scoreboard reset — prior outcomes belong to the previous text",
        })
        if body is not None:
            skill.body = body
        if trigger is not None:
            skill.trigger = trigger
        skill.revision += 1
        skill.outcomes = []
        skill.status = "trial"
        return self.save(skill)

    def retire(self, skill_id: str, reason: str = "") -> Skill | None:
        skill = self.get(skill_id)
        if not skill:
            return None
        skill.status = "retired"
        skill.history.append({"at": _now(), "event": "retired", "reason": reason})
        return self.save(skill)

    # -- retrieval --------------------------------------------------------

    def match(self, context: str, *, subject: str | None = None,
              limit: int = 5, include_unproven: bool = True) -> list[Skill]:
        """
        Skills applicable to a context, best-evidenced first.

        Demoted and retired skills are never returned. A demoted skill is one
        the evidence says is not working; leaving it in the pool "just in case"
        is how a measured negative result gets quietly ignored.
        """
        terms = {t for t in re.split(r"\W+", (context or "").lower()) if len(t) > 2}
        scored = []
        for s in self.all():
            if s.status in ("demoted", "retired"):
                continue
            if s.status == "trial" and not include_unproven:
                continue
            if subject and s.subject and s.subject.upper() != subject.upper():
                continue
            hay = f"{s.name} {s.trigger} {s.body} {s.subject or ''}".lower()
            overlap = sum(1 for t in terms if t in hay)
            if terms and not overlap:
                continue
            scored.append((overlap, s.lower_bound, s))
        scored.sort(key=lambda r: (-r[0], -r[1]))
        return [s for _, _, s in scored[:limit]]

    def report(self) -> dict:
        skills = self.all()
        by_status = {st: [s.to_dict() for s in skills if s.status == st] for st in STATUSES}
        demoted = by_status["demoted"]
        unproven = [s for s in skills if s.trials < MIN_TRIALS and s.status != "retired"]
        return {
            "total": len(skills),
            "by_status": {k: len(v) for k, v in by_status.items()},
            "skills": [s.to_dict() for s in skills],
            "note": (
                f"{len(unproven)} of {len(skills)} skills have fewer than {MIN_TRIALS} "
                f"recorded outcomes and are unproven — their success rates, where shown, "
                f"cannot yet be told apart from luck. "
                + (f"{len(demoted)} have been demoted: measured, and not beating the "
                   f"{BASELINE:.0%} baseline. They are excluded from matching."
                   if demoted else
                   "Nothing has been demoted yet, which on a young library means "
                   "not enough evidence rather than nothing failing.")
            ),
        }
