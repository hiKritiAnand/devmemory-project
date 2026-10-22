"""Deterministic conflict detection between decisions that apply to the same
file/symbol/scope. No LLM call by default -- two explicit rules, each
self-contained and testable without a model:

- `status_overlap` (confidence=high): two *active* (proposed/accepted)
  decisions over the same target, neither superseding the other. Any two
  live decisions about the same thing are worth a human's attention even if
  their text doesn't obviously contradict -- that's the point of surfacing
  it rather than silently picking one.
- `keyword_negation` (confidence=medium): one decision's text negates
  ("not"/"never"/"avoid"/"don't"/"stop using") a term the other asserts
  plainly, over shared vocabulary. This is a blunt heuristic, not NLP --
  real false positives/negatives are expected and the keyword list is
  deliberately small and explicit rather than oversold as language
  understanding.

`--llm-arbitrate` (opt-in, never on by default) sends only `keyword_negation`
candidates to an LLM for a second opinion, reusing extract.py's `run_llm`
subprocess pattern -- `status_overlap` is already high-confidence and
doesn't need arbitration.
"""

import json
import re
from dataclasses import dataclass

from . import extract, storage

ACTIVE_STATUSES = {"proposed", "accepted"}

NEGATION_KEYWORDS = ("not", "never", "avoid", "don't", "stop using")

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "in", "on", "and", "or", "is", "it",
    "this", "that", "with", "was", "be", "as", "at", "by", "we", "our",
    "use", "used", "using", "instead",
}

ARBITRATION_SCHEMA = {
    "type": "object",
    "properties": {
        "conflicts": {"type": "boolean"},
        "explanation": {"type": "string"},
    },
    "required": ["conflicts"],
}

ARBITRATION_PROMPT = """Two development decisions were recorded for the same code scope and a \
keyword heuristic flagged them as possibly contradictory. Decide whether they \
genuinely conflict (one says to do something, the other says not to, or \
they're otherwise incompatible) as opposed to being complementary or \
unrelated despite sharing some wording.

Decision #{id_a}: {summary_a}
Why: {why_a}

Decision #{id_b}: {summary_b}
Why: {why_b}

Respond with JSON only: {{"conflicts": true|false, "explanation": "<one sentence>"}}
"""


@dataclass
class Conflict:
    rule: str
    confidence: str
    decision_a: dict
    decision_b: dict
    reason: str


def _applies_to(decision: dict, file: str, symbol: str | None) -> bool:
    if decision["file"] == file and (
        symbol is None or decision.get("symbol") is None or decision["symbol"] == symbol
    ):
        return True
    scope = decision.get("scope")
    if scope and file.startswith(scope.rstrip("/") + "/"):
        return True
    return False


def _is_active(decision: dict) -> bool:
    return decision.get("status") in ACTIVE_STATUSES


def _already_linked_by_supersession(a: dict, b: dict) -> bool:
    return a.get("superseded_by") == b["id"] or b.get("superseded_by") == a["id"]


def _status_overlap_conflict(a: dict, b: dict) -> Conflict | None:
    if not (_is_active(a) and _is_active(b)):
        return None
    return Conflict(
        rule="status_overlap",
        confidence="high",
        decision_a=a,
        decision_b=b,
        reason=f"both decisions are active ({a['status']} / {b['status']}) for the same target",
    )


def _words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", (text or "").lower()) if w not in _STOPWORDS and len(w) > 2}


def _has_negation(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(neg in lowered for neg in NEGATION_KEYWORDS)


def _keyword_negation_conflict(a: dict, b: dict) -> Conflict | None:
    if not (_is_active(a) and _is_active(b)):
        return None

    text_a = f"{a.get('summary') or ''} {a.get('why') or ''}"
    text_b = f"{b.get('summary') or ''} {b.get('why') or ''}"
    a_negates, b_negates = _has_negation(text_a), _has_negation(text_b)
    if a_negates == b_negates:
        # Need exactly one side negating and the other asserting plainly --
        # both or neither negating isn't this rule's signal.
        return None

    negating, asserting = (a, b) if a_negates else (b, a)
    negating_text, asserting_text = (text_a, text_b) if a_negates else (text_b, text_a)
    shared = _words(asserting_text) & _words(negating_text)
    if not shared:
        return None

    return Conflict(
        rule="keyword_negation",
        confidence="medium",
        decision_a=asserting,
        decision_b=negating,
        reason=(
            f"shared term(s) {sorted(shared)} asserted plainly in #{asserting['id']} "
            f"but negated in #{negating['id']}"
        ),
    )


def _default_arbitration_command() -> list[str]:
    return [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(ARBITRATION_SCHEMA),
        "--tools", "",
        "--permission-mode", "dontAsk",
    ]


def _arbitrate_with_llm(conflict: Conflict, llm_command: list[str] | None = None) -> Conflict | None:
    prompt = ARBITRATION_PROMPT.format(
        id_a=conflict.decision_a["id"], summary_a=conflict.decision_a.get("summary") or "",
        why_a=conflict.decision_a.get("why") or "",
        id_b=conflict.decision_b["id"], summary_b=conflict.decision_b.get("summary") or "",
        why_b=conflict.decision_b.get("why") or "",
    )
    command = llm_command if llm_command is not None else _default_arbitration_command()
    try:
        result = extract.run_llm(prompt, llm_command=command)
    except Exception:
        return conflict  # arbitration failing shouldn't hide the heuristic finding

    if not result.get("conflicts", True):
        return None  # LLM says no real conflict -- drop the heuristic finding
    conflict.reason = result.get("explanation") or conflict.reason
    conflict.confidence = "high"
    return conflict


def detect_conflicts(
    root,
    file: str,
    symbol: str | None = None,
    llm_arbitrate: bool = False,
    llm_command: list[str] | None = None,
) -> list[Conflict]:
    """Returns conflicts among decisions that apply to `file`/`symbol`
    (directly, or via a decision's broader `scope`). Never invokes an LLM
    unless `llm_arbitrate=True` is passed explicitly, and even then only for
    `keyword_negation` candidates -- `status_overlap` is reported as-is.
    """
    candidates = [d for d in storage.query_decisions(root) if _applies_to(d, file, symbol)]

    conflicts: list[Conflict] = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = candidates[i], candidates[j]
            if _already_linked_by_supersession(a, b):
                continue

            # keyword_negation is checked first: when the text itself looks
            # contradictory that's the more informative signal, so a pair
            # is reported once, as keyword_negation, not also as the more
            # generic status_overlap. status_overlap is the fallback for an
            # active overlapping pair whose text doesn't obviously clash --
            # still worth a look, just with less specific evidence behind it
            # (which is why it's *higher* confidence that two live decisions
            # exist, not higher confidence that they truly conflict).
            keyword_conflict = _keyword_negation_conflict(a, b)
            if keyword_conflict is not None:
                if llm_arbitrate:
                    keyword_conflict = _arbitrate_with_llm(keyword_conflict, llm_command=llm_command)
                if keyword_conflict is not None:
                    conflicts.append(keyword_conflict)
                continue

            status_conflict = _status_overlap_conflict(a, b)
            if status_conflict is not None:
                conflicts.append(status_conflict)

    return conflicts
