"""Local/cloud routing policy (design §6.4).

The design lists three escalation triggers: an explicit user request, a task
that is too hard for the small local model, and a confidence check on the
local answer. This module implements all three in one place so a turn's route
and its reason are inspectable (the orb shows the reason in its trace card).

Deliberately conservative: when no cloud key is configured, everything stays
local and says so, rather than failing the turn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

EXPLICIT_ESCALATION = re.compile(
    r"\b(think hard|think deeply|escalate|use the cloud|use agnes|cloud only|"
    r"best model| smarter answer)\b",
    re.I,
)

# Multi-step / analytical work the 1.7b local brain routinely under-delivers on.
COMPLEX_TASK = re.compile(
    r"\b(plan|design|architect|refactor|debug|root cause|compare|versus|vs\.?|"
    r"analyse|analyze|evaluate|trade-?offs?|strategy|roadmap|proof|prove|"
    r"step-by-step|multi-step|summarise|summarize|why does|why is|explain why)\b",
    re.I,
)

# Questions that can only be answered by reading the user's own data through
# tools. Observed on this machine: the 1.7b local model repeatedly claimed it
# could not find something without calling a tool, so asking it first just
# spends tens of seconds before the cloud answers the same question properly.
NEEDS_USER_DATA = re.compile(
    r"\b(my|our) (project|projects|deadline|deadlines|document|documents|file|files|"
    r"notes|calendar|email|emails|tasks|index)\b|"
    r"\bin my (files|docs|documents|notes|project)\b|"
    r"\b(according to my|i (asked|told|said)|what did (i|we) (say|tell|ask))\b|"
    r"\bwhat (are|is) my (deadline|open tasks|projects)\b|"
    r"\b(look at|check|read|see) (my|the) (screen|display|monitor)\b|"
    r"\bwhat (do|can) you see\b",
    re.I,
)

# Questions whose answer lives in the changing outside world rather than in
# the model's weights or the user's own files. Kept deliberately coarse: a
# false positive costs one extra search, a false negative costs a confident
# invention spoken out loud.
NEEDS_LIVE_FACTS = re.compile(
    r"\b(news|headlines?|breaking|latest|currently|happening|tonight|this week|"
    r"this month|today's|weather|forecast|temperature|rain|raining|snow|snowing|"
    r"price|prices|cost|stock|stocks|market|bitcoin|crypto|exchange rate|"
    r"score|scores|winner|champion|released|release|launch|launched|announced|"
    r"announcement|out now|as of)\b|"
    r"\b(right now|just now|at the moment|these days|this year|last night|"
    r"yesterday|today)\b",
    re.I,
)

# A model that says it will go and look, without calling a tool, has promised
# an action it is not taking. Live finding: "I will look at your screen and
# inform you of any errors" was the entire reply.
#
# The verb after the promise must be one the tools actually perform, so an
# ordinary "I can help with that later" is not treated as a failure.
PROMISES_ACTION = re.compile(
    r"^\s*(?:(?:sure|okay|ok|alright|yes),?\s*)?"
    r"(?:i ?'{1,2}ll|i (?:will|am going to|would|can|should|need to|have to)|"
    r"let me|i'{1,2}d better)\s+"
    r"(?:look|check|see|read|search|find|open|fetch|scan|pull|get|review|verify|confirm|triage)\b",
    re.I,
)

# Answers that mean the local model did not actually help.
# Models emit typographic apostrophes (’), so matching happens after
# normalization — a plain ASCII pattern silently fails on "couldn’t".
LOW_CONFIDENCE = re.compile(
    r"^\s*(i (do not|don't|cannot|can't) know|i'?m not sure|as an ai|"
    r"i have no (idea|way to know)|unable to|sorry|i (could not|couldn't) (find|answer|say|tell|verify)|"
    r"i (can'?t|cannot)(?: \w+ly)? (access|find|see|read|open|check|verify|tell you|determine|answer|say)|"
    r"i (was|were) ?(not|n't) able to|"
    r"i don't have (access|information|the|way))\b",
    re.I,
)


def _ascii_text(text: str) -> str:
    return (text or "").replace("\u2019", "'").replace("\u2018", "'").strip()


def promises_unkept_action(answer: str) -> bool:
    return bool(PROMISES_ACTION.match(_ascii_text(answer)))


def looks_like_screen_request(text: str) -> bool:
    """Does this utterance ask what is on the user's screen?

    The canonical regex lives with the vision code (jarvis.vision.describe);
    routing reaches it lazily because importing that module pulls httpx in,
    and this file must stay importable from a bare unit test. Without a hard
    dependency the conservative pattern here is a superset of nothing — if
    the import ever fails we simply skip the steer, exactly the pre-feature
    behaviour.
    """
    try:
        from jarvis.vision.describe import looks_like_screen_request as _impl
        return _impl(text)
    except Exception:
        return False


def looks_like_research_request(text: str) -> bool:
    """Does this question only the outside world can answer right now?

    Live finding: asked for "the top three technology headlines right now" the
    model answered in 4 s from training data ("...as of late 2025") and never
    called the search tool sitting in front of it. Questions about facts that
    move - news, prices, weather, releases, results - are steered to the tool
    the same way screen questions are, and questions about the user's OWN data
    are left to the data tools so the two never fight over the same turn.
    """
    t = text or ""
    if not t.strip():
        return False
    if NEEDS_USER_DATA.search(t) or looks_like_screen_request(t):
        return False
    return bool(NEEDS_LIVE_FACTS.search(t))


def tool_nudge_needed(answer: str, *, used_tools: bool) -> bool:
    """Should this answer be sent back with "actually use the tool"?

    Covers both failure shapes seen live: claiming it cannot look, and
    promising to look while doing nothing.
    """
    if used_tools:
        return False
    text = _ascii_text(answer)
    if not text:
        return False
    return bool(LOW_CONFIDENCE.match(text)) or promises_unkept_action(text)


@dataclass(slots=True)
class Route:
    engine: str          # "local" | "cloud"
    reason: str

    @property
    def is_cloud(self) -> bool:
        return self.engine == "cloud"


def route_turn(
    text: str,
    *,
    force_cloud: bool = False,
    cloud_available: bool = True,
    local_tokens_per_second: float | None = None,
    min_useful_tps: float = 4.0,
) -> Route:
    """Pick the brain for a turn and say why.

    Since the hosted ladder (Gemini 3.5 -> 2.5 -> Agnes, jarvis.agent.
    providers) replaced the Ollama model, "local" means the normal chain
    and "cloud" means deliberately switch to the OTHER brain (Agnes): the
    deep-mode checkbox, an explicit "use the cloud model" request, and the
    confidence retry inside run_turn. User-data and analytical questions no
    longer escalate - the chain leads with a flagship model and every turn
    carries tools and semantic recall anyway. The retired complexity /
    long-form / slow-local heuristics existed only to dodge a weak 1.7B
    model; with no weak model they mis-ran good turns.
    """
    if force_cloud:
        return Route("cloud", "user asked for the other brain") if cloud_available else Route(
            "local", "cloud requested but no fallback key configured"
        )
    if not cloud_available:
        return Route("local", "ladder only (no second-brain key configured)")
    if EXPLICIT_ESCALATION.search(text):
        return Route("cloud", "explicit escalation request")
    return Route("local", "ladder: gemini chain")


def needs_retry(answer: str, *, words_in_question: int, used_tools: bool = False) -> bool:
    """§6.4 confidence check: is the local answer too weak to ship?

    When the turn already consulted the user's own data and reported what that
    data says (including "nothing matches"), the answer is honest rather than
    weak — re-asking the cloud would only cost time.

    Brevity is deliberately NOT treated as weakness. Live finding: the local
    model answered "what is two plus two" with "Four." and the old "fewer than
    three words means a miss" rule escalated that correct answer to the cloud.
    A short right answer is still right, and a needless escalation costs both
    latency and the owner's API quota.
    """
    if words_in_question < 3:
        return False
    text = _ascii_text(answer)
    if not text:
        return True
    if LOW_CONFIDENCE.match(text):
        return not used_tools
    return False

