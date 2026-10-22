import subprocess

from devmemory import knowledge, storage


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


def test_impact_reports_transitive_callers_by_depth(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.impact(git_repo, file="chain.py", symbol="inner")

    assert result["found"] is True
    assert [e["symbol"] for e in result["levels"][0]] == ["middle"]
    assert [e["symbol"] for e in result["levels"][1]] == ["outer"]


def test_impact_respects_max_depth(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.impact(git_repo, file="chain.py", symbol="inner", max_depth=1)

    assert len(result["levels"]) == 1
    assert [e["symbol"] for e in result["levels"][0]] == ["middle"]


def test_impact_includes_decision_count_for_impacted_symbol(git_repo):
    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    knowledge.scan(git_repo)
    storage.add_decision(git_repo, file="chain.py", symbol="middle", summary="Why middle does what it does")

    result = knowledge.impact(git_repo, file="chain.py", symbol="inner")

    middle_entry = next(e for e in result["levels"][0] if e["symbol"] == "middle")
    assert middle_entry["decision_count"] == 1


def test_impact_lists_importing_files_separately_from_levels(git_repo):
    _write(git_repo, "lib.py", "def helper():\n    return 1\n")
    _write(git_repo, "consumer.py", "from .lib import helper\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.impact(git_repo, file="lib.py", symbol="helper")

    assert result["levels"] == []  # consumer.py imports the file but never calls helper()
    assert "consumer.py" in result["importing_files"]


def test_impact_on_symbol_with_no_callers_is_empty_not_an_error(git_repo):
    _write(git_repo, "standalone.py", "def lonely():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.impact(git_repo, file="standalone.py", symbol="lonely")

    assert result["found"] is True
    assert result["levels"] == []
    assert result["importing_files"] == []


def test_impact_reports_not_found_for_unknown_symbol(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.impact(git_repo, file="a.py", symbol="nope")
    assert result["found"] is False


def test_cli_knowledge_impact_logs_access_and_prints_depths(git_repo, capsys):
    from devmemory.cli import main

    _write(git_repo, "chain.py", CHAIN)
    _commit(git_repo)
    main(["--path", str(git_repo), "init"])
    capsys.readouterr()
    main(["--path", str(git_repo), "knowledge", "scan"])
    capsys.readouterr()

    main(["--path", str(git_repo), "knowledge", "impact", "--file", "chain.py", "--symbol", "inner"])
    out = capsys.readouterr().out

    assert "Depth 1:" in out
    assert "middle" in out
    assert "Depth 2:" in out
    assert "outer" in out

    conn = storage._connect(git_repo)
    try:
        rows = conn.execute("SELECT command FROM access_log WHERE command = 'impact'").fetchall()
    finally:
        conn.close()
    assert len(rows) == 2  # middle + outer
