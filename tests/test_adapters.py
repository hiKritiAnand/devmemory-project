import json

from devmemory.adapters import claude as claude_adapter
from devmemory.adapters import cursor as cursor_adapter


def test_claude_install_creates_file(tmp_path):
    paths = claude_adapter.install(tmp_path)

    assert paths[0] == tmp_path / "CLAUDE.md"
    content = paths[0].read_text()
    assert claude_adapter.MARKER_START in content
    assert claude_adapter.MARKER_END in content
    assert "devmemory query" in content
    assert "devmemory record" in content


def test_claude_install_preserves_existing_content(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# My project\n\nSome existing instructions.\n")

    claude_adapter.install(tmp_path)

    content = claude_md.read_text()
    assert "# My project" in content
    assert "Some existing instructions." in content
    assert "devmemory" in content


def test_claude_install_is_idempotent_and_updates_in_place(tmp_path):
    claude_adapter.install(tmp_path)
    claude_adapter.install(tmp_path)

    content = (tmp_path / "CLAUDE.md").read_text()
    assert content.count(claude_adapter.MARKER_START) == 1
    assert content.count(claude_adapter.MARKER_END) == 1

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop_hooks = settings["hooks"]["Stop"][0]["hooks"]
    matching = [h for h in stop_hooks if claude_adapter._is_devmemory_stop_hook(h)]
    assert len(matching) == 1


def test_claude_install_writes_stop_hook_in_settings_json(tmp_path):
    paths = claude_adapter.install(tmp_path)

    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path in paths
    settings = json.loads(settings_path.read_text())
    stop_hooks = settings["hooks"]["Stop"][0]["hooks"]
    assert stop_hooks == [
        {
            "type": "command",
            "command": claude_adapter.STOP_HOOK_COMMAND,
            "args": claude_adapter.STOP_HOOK_ARGS,
            "timeout": 60,
        }
    ]


def test_claude_install_registers_mcp_server(tmp_path):
    paths = claude_adapter.install(tmp_path)

    config_path = tmp_path / ".mcp.json"
    assert config_path in paths
    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["devmemory"]["command"] == "devmemory"
    assert "mcp-server" in config["mcpServers"]["devmemory"]["args"]


def test_claude_install_mcp_server_preserves_other_registered_servers(tmp_path):
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {"other-server": {"command": "other", "args": []}}}))

    claude_adapter.install(tmp_path)

    config = json.loads(config_path.read_text())
    assert "other-server" in config["mcpServers"]
    assert "devmemory" in config["mcpServers"]


def test_claude_install_preserves_existing_settings_json_content(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"otherSetting": True, "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "some-other-hook", "args": [], "timeout": 30}
    ]}]}}))

    claude_adapter.install(tmp_path)

    settings = json.loads(settings_path.read_text())
    assert settings["otherSetting"] is True
    stop_hooks = settings["hooks"]["Stop"][0]["hooks"]
    assert {"type": "command", "command": "some-other-hook", "args": [], "timeout": 30} in stop_hooks
    assert any(claude_adapter._is_devmemory_stop_hook(h) for h in stop_hooks)


def test_cursor_install_creates_rules_file(tmp_path):
    paths = cursor_adapter.install(tmp_path)

    path = tmp_path / ".cursor" / "rules" / "devmemory.mdc"
    assert path in paths
    assert path.exists()
    content = path.read_text()
    assert "devmemory" in content
    assert "devmemory query" in content
    assert "devmemory record" in content


def test_cursor_install_registers_mcp_server(tmp_path):
    import json

    cursor_adapter.install(tmp_path)

    config_path = tmp_path / ".cursor" / "mcp.json"
    assert config_path.exists()
    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["devmemory"]["command"] == "devmemory"
    assert "mcp-server" in config["mcpServers"]["devmemory"]["args"]


def test_cursor_install_mcp_server_preserves_other_registered_servers(tmp_path):
    import json

    config_path = tmp_path / ".cursor"
    config_path.mkdir()
    (config_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other-server": {"command": "other", "args": []}}})
    )

    cursor_adapter.install(tmp_path)

    config = json.loads((config_path / "mcp.json").read_text())
    assert "other-server" in config["mcpServers"]
    assert "devmemory" in config["mcpServers"]


def test_cursor_install_is_idempotent(tmp_path):
    cursor_adapter.install(tmp_path)
    first = (tmp_path / ".cursor" / "rules" / "devmemory.mdc").read_text()

    cursor_adapter.install(tmp_path)
    second = (tmp_path / ".cursor" / "rules" / "devmemory.mdc").read_text()

    assert first == second
