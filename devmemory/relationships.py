"""Extensible relationship/edge-kind registry.

The spec's full relationship taxonomy (CONTAINS, IMPORTS, CALLS, ..., SUPERSEDES,
DEPENDS_ON) maps onto what devmemory already stores plus a small number of
genuinely new producers. Most of the taxonomy is *derived* -- already
expressible via an existing stored fact, or computed at query time -- rather
than duplicated as a new stored edge. This module is the single place that
states, for every spec-named kind, which disposition applies and why, so the
mapping isn't scattered as hardcoded strings across cli.py/knowledge.py/
storage.py. A test (test_relationships.py) asserts every spec-named kind
resolves to an entry here, so none is silently dropped.
"""

from dataclasses import dataclass

STORAGE_KINDS = ("edges", "decision_links", "derived")


@dataclass(frozen=True)
class EdgeKindSpec:
    name: str  # the spec's name, e.g. "EXTENDS"
    storage: str  # "edges" | "decision_links" | "derived"
    src_entity: str  # "node" | "decision" | "commit" | "context_record"
    dst_entity: str
    producer: str  # where this fact comes from, for a human reading the registry
    legacy_alias: str | None = None  # the existing lowercase devmemory kind this maps to, if any

    def __post_init__(self):
        if self.storage not in STORAGE_KINDS:
            raise ValueError(f"invalid storage {self.storage!r} for {self.name!r}; must be one of {STORAGE_KINDS}")


REGISTRY: dict[str, EdgeKindSpec] = {
    "CONTAINS": EdgeKindSpec(
        "CONTAINS", "derived", "node", "node",
        "derived from the existing 'defines' edge (file -> symbol)", legacy_alias="defines",
    ),
    "IMPORTS": EdgeKindSpec(
        "IMPORTS", "edges", "node", "node",
        "knowledge.py scan(): per-language import resolution", legacy_alias="imports",
    ),
    "CALLS": EdgeKindSpec(
        "CALLS", "edges", "node", "node",
        "knowledge.py scan(): same-file syntactic call resolution", legacy_alias="calls",
    ),
    "READS": EdgeKindSpec(
        "READS", "derived", "node", "node",
        "not separately tracked -- no producer distinguishes a read from a reference; see USES_SCHEMA",
    ),
    "WRITES": EdgeKindSpec(
        "WRITES", "derived", "node", "node",
        "not separately tracked -- same limitation as READS",
    ),
    "IMPLEMENTS": EdgeKindSpec(
        "IMPLEMENTS", "edges", "node", "node",
        "knowledge.py scan(): per-language implements-clause resolution", legacy_alias="implements",
    ),
    "EXTENDS": EdgeKindSpec(
        "EXTENDS", "edges", "node", "node",
        "knowledge.py scan(): per-language extends-clause resolution", legacy_alias="extends",
    ),
    "EXPOSES": EdgeKindSpec(
        "EXPOSES", "derived", "node", "node",
        "not separately tracked -- devmemory has no API-endpoint node kind yet",
    ),
    "USES_SCHEMA": EdgeKindSpec(
        "USES_SCHEMA", "derived", "node", "node",
        "derived from the existing 'references' edge where the destination node's kind is 'table'",
        legacy_alias="references",
    ),
    "TESTS": EdgeKindSpec(
        "TESTS", "edges", "node", "node",
        "knowledge.py scan(): a test file's existing calls/imports are double-tagged via is_test_file()",
        legacy_alias="tests",
    ),
    "CONSTRAINED_BY": EdgeKindSpec(
        "CONSTRAINED_BY", "derived", "node", "decision",
        "computed at query time from decisions.scope/file/symbol matching a node, not stored",
    ),
    "DOCUMENTED_BY": EdgeKindSpec(
        "DOCUMENTED_BY", "decision_links", "node", "context_record",
        "onboard.py's link_to_nodes()",
    ),
    "APPLIES_TO": EdgeKindSpec(
        "APPLIES_TO", "derived", "context_record", "node",
        "the inverse of CONSTRAINED_BY/DOCUMENTED_BY, computed the same way, not separately stored",
    ),
    "CHANGED_BY": EdgeKindSpec(
        "CHANGED_BY", "decision_links", "node", "commit",
        "add_decision()'s existing session_ref convention, surfaced via decision_links",
    ),
    "INTRODUCED_BY": EdgeKindSpec(
        "INTRODUCED_BY", "decision_links", "decision", "commit",
        "storage.add_decision(): written when session_ref follows the 'commit:<sha>' convention",
    ),
    "SUPERSEDES": EdgeKindSpec(
        "SUPERSEDES", "decision_links", "decision", "decision",
        "storage.supersede_decision()",
    ),
    "DEPENDS_ON": EdgeKindSpec(
        "DEPENDS_ON", "derived", "node", "node",
        "derived from the existing 'imports' edge", legacy_alias="imports",
    ),
}


def all_kinds() -> list[str]:
    return sorted(REGISTRY)


def spec_for(name: str) -> EdgeKindSpec:
    try:
        return REGISTRY[name.upper()]
    except KeyError:
        raise ValueError(f"unknown relationship kind: {name!r}; known kinds: {all_kinds()}") from None
