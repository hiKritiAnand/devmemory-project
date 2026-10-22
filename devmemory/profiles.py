"""Named, deterministic retrieval policies ("traversal profiles"): which
edge kinds to follow, how deep, and what else to pull in, selected by task
rather than one fixed BFS for every query. Each profile is a plain,
inspectable config -- devmemory/retrieval.py's traverse() is the single
traversal implementation every profile shares; a profile never gets its own
bespoke graph algorithm.

Exact edge_kinds/max_depth values here are a starting judgment call with no
real-usage data behind them yet -- expect to retune once these are actually
used, same honesty as devmemory's token-budget default elsewhere.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TraversalProfile:
    name: str
    edge_kinds: tuple[str, ...]  # which edge kinds to follow, incoming-direction BFS
    max_depth: int = 3
    include_decision_links: bool = False
    include_context_records: bool = False
    include_importers: bool = True  # the separate, lower-confidence "imports this file" list
    include_history: bool = False


PROFILES: dict[str, TraversalProfile] = {
    "explanation": TraversalProfile(
        "explanation", edge_kinds=("calls", "references"), max_depth=1,
        include_decision_links=True, include_context_records=True, include_importers=True,
    ),
    "bug_fix": TraversalProfile(
        "bug_fix", edge_kinds=("calls", "references", "tests"), max_depth=2,
        include_decision_links=True, include_history=True, include_importers=True,
    ),
    "feature": TraversalProfile(
        "feature", edge_kinds=("calls", "references", "extends", "implements"), max_depth=2,
        include_decision_links=True, include_context_records=True, include_importers=True,
    ),
    "refactor": TraversalProfile(
        "refactor", edge_kinds=("calls", "references", "extends", "implements"), max_depth=3,
        include_decision_links=True, include_importers=True, include_history=True,
    ),
    "change_impact": TraversalProfile(
        # Matches knowledge.impact()'s pre-existing behavior exactly: calls/
        # references only, depth 3, no decision_links/context/history.
        "change_impact", edge_kinds=("calls", "references"), max_depth=3,
        include_decision_links=False, include_importers=True,
    ),
    "security_sensitive": TraversalProfile(
        "security_sensitive", edge_kinds=("calls", "references", "extends", "implements", "tests"), max_depth=4,
        include_decision_links=True, include_context_records=True, include_importers=True, include_history=True,
    ),
    "architecture_exploration": TraversalProfile(
        "architecture_exploration", edge_kinds=("extends", "implements"), max_depth=2,
        include_decision_links=True, include_context_records=True, include_importers=False,
    ),
    "historical": TraversalProfile(
        "historical", edge_kinds=("calls", "references"), max_depth=1,
        include_decision_links=True, include_history=True, include_context_records=False, include_importers=False,
    ),
}


def get(name: str) -> TraversalProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown traversal profile {name!r}; known profiles: {all_names()}") from None


def all_names() -> list[str]:
    return sorted(PROFILES)
