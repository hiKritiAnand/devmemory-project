"""Tests for the MCP server. Skipped entirely if the optional 'mcp' package
isn't installed (`pip install devmemory[mcp]`) -- consistent with it being
an optional extra, not a base dependency.
"""

import asyncio
import json
import subprocess

import pytest

mcp = pytest.importorskip("mcp")

from devmemory import knowledge, mcp_server, storage  # noqa: E402


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit(root):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit"], cwd=root, check=True)


def _call(server, name, args):
    async def _run():
        return await server.call_tool(name, args)
    return asyncio.run(_run())


def _result_dict(result) -> dict:
    text = result.content[0].text
    return json.loads(text)


def test_tools_list_returns_exactly_six_tools_with_schemas(tmp_path):
    storage.init_store(tmp_path)
    server = mcp_server.build_server(tmp_path)

    async def _list():
        return await server.list_tools()

    tools = asyncio.run(_list())
    names = {t.name for t in tools}
    assert names == {
        "devmemory_search", "devmemory_explain", "devmemory_impact",
        "devmemory_context", "devmemory_history", "devmemory_record",
    }
    for t in tools:
        assert t.input_schema is not None


def test_devmemory_record_tool_persists_a_decision(tmp_path):
    storage.init_store(tmp_path)
    server = mcp_server.build_server(tmp_path)

    result = _call(server, "devmemory_record", {"file": "a.py", "summary": "Use approach A"})
    data = _result_dict(result)

    assert data["recorded"]["file"] == "a.py"
    assert data["conflicts"] == []
    assert len(storage.query_decisions(tmp_path, file="a.py")) == 1


def test_devmemory_record_tool_surfaces_conflicts_not_swallowed(tmp_path):
    storage.init_store(tmp_path)
    server = mcp_server.build_server(tmp_path)

    _call(server, "devmemory_record", {
        "file": "a.py", "symbol": "foo", "summary": "Use synchronous retries", "status": "accepted",
    })
    result = _call(server, "devmemory_record", {
        "file": "a.py", "symbol": "foo", "summary": "Never use synchronous retries", "status": "accepted",
    })
    data = _result_dict(result)

    assert len(data["conflicts"]) == 1
    assert data["conflicts"][0]["rule"] == "keyword_negation"


def test_devmemory_search_tool_finds_recorded_decisions(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", summary="uses synchronous retries for payments")
    server = mcp_server.build_server(tmp_path)

    result = _call(server, "devmemory_search", {"text": "synchronous"})
    data = _result_dict(result)

    assert data["included"] == 1
    assert "synchronous" in data["text"]


def test_devmemory_impact_and_cli_context_change_impact_go_through_identical_path(git_repo):
    _write(git_repo, "chain.py", "def outer():\n    return middle()\n\ndef middle():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    server = mcp_server.build_server(git_repo)
    mcp_result = _result_dict(_call(server, "devmemory_impact", {"file": "chain.py", "symbol": "middle"}))

    from devmemory import retrieval
    direct_result = retrieval.traverse(git_repo, "chain.py", "middle", profile_name="change_impact")

    assert mcp_result["found"] is True
    assert "outer" in mcp_result["text"]
    assert direct_result["found"] is True
    assert any(e["symbol"] == "outer" for level in direct_result["levels"] for e in level)


def test_devmemory_context_tool_with_profile(git_repo):
    _write(git_repo, "chain.py", "def outer():\n    return middle()\n\ndef middle():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)
    server = mcp_server.build_server(git_repo)

    result = _call(server, "devmemory_context", {"file": "chain.py", "symbol": "middle", "profile": "change_impact"})
    data = _result_dict(result)
    assert data["found"] is True
    assert data["profile"] == "change_impact"


def test_devmemory_explain_tool_file_level_without_symbol(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)
    server = mcp_server.build_server(git_repo)

    result = _call(server, "devmemory_explain", {"file": "a.py"})
    data = _result_dict(result)
    assert data["found"] is True
    assert data["node"]["file"] == "a.py"


def test_devmemory_history_tool_decision_history(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")
    server = mcp_server.build_server(tmp_path)

    result = _call(server, "devmemory_history", {"decision_id": record["id"]})
    data = _result_dict(result)
    assert len(data["decision_history"]) == 1
    assert data["decision_history"][0]["event"] == "created"


def test_devmemory_history_tool_node_history(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)
    _write(git_repo, "a.py", "def foo():\n    '''now documented'''\n    return 1\n")
    _commit(git_repo)
    knowledge.scan(git_repo)

    server = mcp_server.build_server(git_repo)
    result = _call(server, "devmemory_history", {"file": "a.py", "symbol": "foo"})
    data = _result_dict(result)
    assert len(data["node_history"]) == 1


def test_cli_mcp_server_without_mcp_extra_prints_install_hint_and_exits(monkeypatch, capsys, tmp_path):
    from devmemory import cli

    def _raise_missing_mcp():
        raise ImportError("The devmemory MCP server requires the optional 'mcp' package. Install it with: pip install devmemory[mcp]")

    monkeypatch.setattr(cli, "_import_mcp_server", _raise_missing_mcp)

    with pytest.raises(SystemExit):
        cli.main(["--path", str(tmp_path), "mcp-server"])

    err = capsys.readouterr().err
    assert "pip install devmemory[mcp]" in err
