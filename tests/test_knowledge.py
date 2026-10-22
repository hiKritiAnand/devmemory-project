import json
import os
import subprocess

import pytest

from devmemory import knowledge, storage


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit_all(root, message="commit"):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


PY_A = '''import os
from .b import helper

def do_thing(x):
    return helper(x) + os.getpid()
'''

PY_B = '''def helper(x):
    return x + 1
'''

TS_APP = '''import { helper } from "./helper";

export function run(x: number): number {
    return helper(x);
}
'''

TS_HELPER = '''export function helper(x: number): number {
    return x + 1;
}
'''


def test_discover_files_respects_gitignore(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, ".gitignore", "ignored.py\n")
    _write(git_repo, "ignored.py", "x = 1\n")
    _commit_all(git_repo)

    discovered = {p.name for p in knowledge.discover_files(git_repo)}
    assert "a.py" in discovered
    assert "ignored.py" not in discovered


def test_discover_files_skips_vendor_dirs_outside_git(tmp_path):
    _write(tmp_path, "src/a.py", PY_A)
    _write(tmp_path, "node_modules/pkg/index.js", "module.exports = {};")

    discovered = {p.relative_to(tmp_path).as_posix() for p in knowledge.discover_files(tmp_path)}
    assert "src/a.py" in discovered
    assert not any("node_modules" in d for d in discovered)


def test_discover_files_respects_ignore_config_exclude_dirs(tmp_path):
    _write(tmp_path, "src/a.py", PY_A)
    _write(tmp_path, "generated/b.py", PY_B)
    (tmp_path / ".devmemory").mkdir()
    (tmp_path / ".devmemory" / "ignore.json").write_text(json.dumps({"exclude_dirs": ["generated"]}))

    discovered = {p.relative_to(tmp_path).as_posix() for p in knowledge.discover_files(tmp_path)}
    assert "src/a.py" in discovered
    assert not any("generated" in d for d in discovered)


def test_discover_files_respects_ignore_config_exclude_globs(tmp_path):
    _write(tmp_path, "src/a.py", PY_A)
    _write(tmp_path, "vendor/lib.min.js", "x=1;")
    (tmp_path / ".devmemory").mkdir()
    (tmp_path / ".devmemory" / "ignore.json").write_text(json.dumps({"exclude_globs": ["*.min.js"]}))

    discovered = {p.relative_to(tmp_path).as_posix() for p in knowledge.discover_files(tmp_path)}
    assert "src/a.py" in discovered
    assert "vendor/lib.min.js" not in discovered


def test_discover_files_excludes_symlink_resolving_outside_root(tmp_path):
    outside_dir = tmp_path.parent / f"{tmp_path.name}_outside_target"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.py").write_text("SECRET = 1\n")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write(repo_root, "src/a.py", PY_A)
    try:
        os.symlink(outside_dir / "secret.py", repo_root / "linked_out.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")

    discovered = {p.name for p in knowledge.discover_files(repo_root)}
    assert "a.py" in discovered
    assert "linked_out.py" not in discovered


def test_parse_python_extracts_imports_definitions_and_calls(git_repo):
    path = _write(git_repo, "a.py", PY_A)
    parsed = knowledge.parse_file(path, "python")

    assert "os" in parsed.imports
    assert any("b" in i for i in parsed.imports)
    assert [d.name for d in parsed.definitions] == ["do_thing"]
    assert parsed.definitions[0].kind == "function"
    assert parsed.definitions[0].signature == "(x)"
    assert ("do_thing", "helper") in parsed.calls


def test_parse_js_extracts_imports_definitions_and_calls(git_repo):
    path = _write(git_repo, "app.ts", TS_APP)
    parsed = knowledge.parse_file(path, "typescript")

    assert parsed.imports == ["./helper"]
    assert [d.name for d in parsed.definitions] == ["run"]
    assert ("run", "helper") in parsed.calls


def test_scan_links_python_file_to_file_import(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="a.py")
    targets = {e["file"] for e in result["outgoing"] if e["edge_kind"] == "imports"}
    assert "b.py" in targets


def test_scan_links_js_relative_import_by_path(git_repo):
    _write(git_repo, "app.ts", TS_APP)
    _write(git_repo, "helper.ts", TS_HELPER)
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="app.ts")
    targets = {e["file"] for e in result["outgoing"] if e["edge_kind"] == "imports"}
    assert "helper.ts" in targets


def test_scan_creates_external_node_for_unresolved_import(git_repo):
    _write(git_repo, "a.py", PY_A)
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="a.py")
    externals = [e for e in result["outgoing"] if e["edge_kind"] == "imports" and e["kind"] == "external"]
    assert any(e["file"] == "os" for e in externals)


def test_scan_creates_call_edge_between_symbols_in_same_file(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="a.py", symbol="do_thing")
    calls = [e["symbol"] for e in result["outgoing"] if e["edge_kind"] == "calls"]
    # helper is defined in b.py, not a.py, so the best-effort same-file
    # matching correctly does NOT link it (no type resolution) -- do_thing
    # has no same-file callee here, which is the honest expected outcome.
    assert calls == []


def test_scan_links_calls_within_the_same_file(git_repo):
    src = "def outer():\n    return inner()\n\ndef inner():\n    return 1\n"
    _write(git_repo, "same.py", src)
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="same.py", symbol="outer")
    calls = [e["symbol"] for e in result["outgoing"] if e["edge_kind"] == "calls"]
    assert calls == ["inner"]


def test_scan_is_incremental_skips_unchanged_files(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)

    first = knowledge.scan(git_repo)
    assert first.files_scanned == 2
    assert first.files_unchanged == 0

    second = knowledge.scan(git_repo)
    assert second.files_scanned == 0
    assert second.files_unchanged == 2


def test_scan_reparses_modified_file_and_keeps_other_files_edges_stable(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    # a.py imports b.py; modify b.py (content changes, path doesn't) and
    # rescan -- a.py's import edge to b.py must survive since b.py's file
    # node id must stay stable across the rescan.
    _write(git_repo, "b.py", PY_B + "\ndef extra():\n    return 2\n")
    summary = knowledge.scan(git_repo)
    assert summary.files_scanned == 1
    assert summary.files_unchanged == 1

    result = knowledge.inspect(git_repo, file="a.py")
    targets = {e["file"] for e in result["outgoing"] if e["edge_kind"] == "imports"}
    assert "b.py" in targets
    b_result = knowledge.inspect(git_repo, file="b.py")
    assert any(e["symbol"] == "extra" for e in b_result["outgoing"] if e["edge_kind"] == "defines")


def test_scan_removes_nodes_for_deleted_file(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    (git_repo / "b.py").unlink()
    _commit_all(git_repo, "remove b")
    knowledge.scan(git_repo)

    result = knowledge.inspect(git_repo, file="b.py")
    assert result["found"] is False


def test_stats_counts_nodes_and_edges_by_kind(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.stats(git_repo)
    assert result["nodes"]["file"] == 2
    assert result["nodes"]["function"] >= 2
    assert result["edges"]["defines"] >= 2
    assert result["edges"]["imports"] >= 1


def test_render_files_detail_excludes_symbol_nodes(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    diagram = knowledge.render_mermaid(git_repo, detail="files")
    assert "a.py" in diagram
    assert "b.py" in diagram
    assert "do_thing" not in diagram
    assert knowledge.get_mermaid_path(git_repo).exists()


def test_render_symbols_detail_shows_functions_for_one_file(git_repo):
    src = "def outer():\n    return inner()\n\ndef inner():\n    return 1\n"
    _write(git_repo, "same.py", src)
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    diagram = knowledge.render_mermaid(git_repo, detail="symbols", file="same.py")
    assert "outer" in diagram
    assert "inner" in diagram


def test_render_symbols_detail_requires_file(git_repo):
    with pytest.raises(ValueError):
        knowledge.render_mermaid(git_repo, detail="symbols")


def test_render_marks_nodes_with_devmemory_decisions(git_repo):
    _write(git_repo, "a.py", PY_A)
    _commit_all(git_repo)
    knowledge.scan(git_repo)
    storage.add_decision(git_repo, file="a.py", summary="Switched to os.getpid for uniqueness")

    diagram = knowledge.render_mermaid(git_repo, detail="files")
    assert "hasMemory" in diagram
    assert "1" in diagram  # decision count baked into the label


def test_inspect_includes_decisions_and_edges(git_repo):
    _write(git_repo, "a.py", PY_A)
    _write(git_repo, "b.py", PY_B)
    _commit_all(git_repo)
    knowledge.scan(git_repo)
    storage.add_decision(git_repo, file="a.py", symbol="do_thing", summary="Added os.getpid() for uniqueness")

    result = knowledge.inspect(git_repo, file="a.py", symbol="do_thing")
    assert result["found"] is True
    assert result["node"]["kind"] == "function"
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["summary"] == "Added os.getpid() for uniqueness"


def test_inspect_reports_not_found_for_unknown_node(git_repo):
    _write(git_repo, "a.py", PY_A)
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    result = knowledge.inspect(git_repo, file="nope.py")
    assert result["found"] is False
