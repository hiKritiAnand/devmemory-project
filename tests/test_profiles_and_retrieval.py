import subprocess

import pytest

from devmemory import knowledge, profiles, retrieval, storage


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit(root):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit"], cwd=root, check=True)


CHAIN = '''
def outer():
    return middle()

def middle():
    return inner()

def inner():
    return 1
'''


def test_all_eight_profiles_are_registered():
    expected = {
        "explanation", "bug_fix", "feature", "refactor", "change_impact",
        "security_sensitive", "architecture_exploration", "historical",
    }
    assert set(profiles.all_names()) == expected


def test_get_unknown_profile_raises_with_valid_list():
    with pytest.raises(ValueError, match="unknown traversal profile"):
        profiles.get("not_a_real_profile")


def test_traverse_unknown_profile_name_errors(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    with pytest.raises(ValueError, match="unknown traversal profile"):
        retrieval.traverse(git_repo, "a.py", "foo", profile_name="bogus")


def test_traverse_change_impact_matches_knowledge_impact_exactly(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)

    via_profile = retrieval.traverse(git_repo, "chain.py", "inner", profile_name="change_impact")
    via_impact = knowledge.impact(git_repo, file="chain.py", symbol="inner")

    assert via_profile["levels"] == via_impact["levels"]
    assert via_profile["importing_files"] == via_impact["importing_files"]
    assert via_profile["node_ids"] == via_impact["node_ids"]


def test_traverse_honors_profile_max_depth(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = retrieval.traverse(git_repo, "chain.py", "inner", profile_name="explanation")
    # "explanation" profile config: max_depth=1
    assert len(result["levels"]) <= 1


def test_traverse_max_depth_override_wins_over_profile_default(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = retrieval.traverse(git_repo, "chain.py", "inner", profile_name="explanation", max_depth_override=5)
    assert len(result["levels"]) == 2  # full chain reached despite explanation's default of 1


def test_traverse_includes_decision_links_only_when_profile_requests_it(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)
    record = storage.add_decision(git_repo, file="a.py", summary="s1", session_ref="commit:deadbeef")

    node_id = knowledge.inspect(git_repo, "a.py", "foo")["node"]["id"]
    storage.add_decision_link(git_repo, "node", str(node_id), "context_record", "1", "DOCUMENTED_BY")

    with_links = retrieval.traverse(git_repo, "a.py", "foo", profile_name="explanation")
    assert any(link["kind"] == "DOCUMENTED_BY" for link in with_links["decision_links"])

    without_links = retrieval.traverse(git_repo, "a.py", "foo", profile_name="change_impact")
    assert without_links["decision_links"] == []


def test_traverse_respects_branch_scope_for_decision_counts(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)
    storage.add_decision(git_repo, file="chain.py", symbol="middle", summary="on default branch")

    subprocess.run(["git", "checkout", "-q", "-b", "feature-branch"], cwd=git_repo, check=True)

    result = retrieval.traverse(git_repo, "chain.py", "inner", profile_name="change_impact")
    middle_entry = next(e for e in result["levels"][0] if e["symbol"] == "middle")
    assert middle_entry["decision_count"] == 0  # branch-scoped: not visible from feature-branch

    result_all = retrieval.traverse(git_repo, "chain.py", "inner", profile_name="change_impact", all_branches=True)
    middle_entry_all = next(e for e in result_all["levels"][0] if e["symbol"] == "middle")
    assert middle_entry_all["decision_count"] == 1


def test_traverse_not_found_returns_profile_name_too(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = retrieval.traverse(git_repo, "a.py", "nope", profile_name="bug_fix")
    assert result["found"] is False
    assert result["profile"] == "bug_fix"
