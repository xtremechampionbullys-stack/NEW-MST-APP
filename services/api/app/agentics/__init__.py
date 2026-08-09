"""
The agentic harness: session state, memory, and skills.

Modelled on the published Hermes Agent architecture (NousResearch/hermes-agent):
SQLite state with dual FTS5 indexes, always-active MEMORY.md and USER.md with a
four-phase provider interface, and skills created from experience that improve
during use.

Where the upstream source is explicit this follows it. Where it is not, the
documented behaviour is implemented and the gap is stated in the module
docstring rather than filled with an invented detail presented as original.

The domain additions — provenance and decay on every fact, and a Wilson lower
bound gating skill promotion — are noted as additions. They exist because the
consequences here are financial: a memory that cannot say how it learned
something, or a skill that cannot conclude it is not working, is worse than no
memory and no skill.
"""

from .memory import BuiltinMemoryProvider, Fact, MemoryManager, MemoryStore
from .skills import Skill, SkillLibrary, wilson_lower_bound
from .state import Message, SessionDB

__all__ = ["BuiltinMemoryProvider", "Fact", "MemoryManager", "MemoryStore", "Skill", "SkillLibrary", "SessionDB",
           "Message", "wilson_lower_bound"]
