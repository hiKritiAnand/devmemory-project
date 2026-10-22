import pytest

from devmemory import relationships

SPEC_NAMED_KINDS = [
    "CONTAINS", "IMPORTS", "CALLS", "READS", "WRITES", "IMPLEMENTS", "EXTENDS",
    "EXPOSES", "USES_SCHEMA", "TESTS", "CONSTRAINED_BY", "DOCUMENTED_BY",
    "APPLIES_TO", "CHANGED_BY", "INTRODUCED_BY", "SUPERSEDES", "DEPENDS_ON",
]


@pytest.mark.parametrize("kind", SPEC_NAMED_KINDS)
def test_every_spec_named_kind_resolves_to_an_entry(kind):
    spec = relationships.spec_for(kind)
    assert spec.name == kind
    assert spec.storage in relationships.STORAGE_KINDS


def test_spec_for_is_case_insensitive():
    assert relationships.spec_for("extends").name == "EXTENDS"


def test_spec_for_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown relationship kind"):
        relationships.spec_for("NOT_A_REAL_KIND")


def test_all_kinds_covers_every_spec_named_kind():
    assert set(SPEC_NAMED_KINDS) <= set(relationships.all_kinds())


def test_genuinely_new_producers_are_marked_edges_or_decision_links_not_derived():
    for name in ("EXTENDS", "IMPLEMENTS", "TESTS", "DOCUMENTED_BY", "CHANGED_BY", "INTRODUCED_BY", "SUPERSEDES"):
        spec = relationships.spec_for(name)
        assert spec.storage in ("edges", "decision_links"), f"{name} should be a real producer, not derived"


def test_invalid_storage_value_rejected():
    with pytest.raises(ValueError, match="invalid storage"):
        relationships.EdgeKindSpec("BOGUS", "not_a_real_storage_kind", "node", "node", "test")
