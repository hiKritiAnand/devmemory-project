import json

import pytest

from devmemory.adapters import _json_merge


def _match_devmemory(entry):
    return entry.get("command") == "devmemory-cmd"


def test_merge_hook_creates_settings_file_when_absent(tmp_path):
    settings_path = tmp_path / "settings.json"
    entry = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 60}

    _json_merge.merge_hook(settings_path, "Stop", entry, _match_devmemory)

    data = json.loads(settings_path.read_text())
    assert data["hooks"]["Stop"][0]["hooks"] == [entry]


def test_merge_hook_preserves_unrelated_top_level_keys(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"otherSetting": True}))
    entry = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 60}

    _json_merge.merge_hook(settings_path, "Stop", entry, _match_devmemory)

    data = json.loads(settings_path.read_text())
    assert data["otherSetting"] is True
    assert data["hooks"]["Stop"][0]["hooks"] == [entry]


def test_merge_hook_preserves_other_entries_in_same_event_group(tmp_path):
    settings_path = tmp_path / "settings.json"
    other_entry = {"type": "command", "command": "some-other-hook", "args": [], "timeout": 30}
    settings_path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [other_entry]}]}}))
    entry = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 60}

    _json_merge.merge_hook(settings_path, "Stop", entry, _match_devmemory)

    data = json.loads(settings_path.read_text())
    hooks = data["hooks"]["Stop"][0]["hooks"]
    assert other_entry in hooks
    assert entry in hooks


def test_merge_hook_is_idempotent_no_duplicate_entries(tmp_path):
    settings_path = tmp_path / "settings.json"
    entry = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 60}

    _json_merge.merge_hook(settings_path, "Stop", entry, _match_devmemory)
    _json_merge.merge_hook(settings_path, "Stop", entry, _match_devmemory)

    data = json.loads(settings_path.read_text())
    matching = [h for h in data["hooks"]["Stop"][0]["hooks"] if _match_devmemory(h)]
    assert len(matching) == 1


def test_merge_hook_updates_entry_in_place_on_changed_timeout(tmp_path):
    settings_path = tmp_path / "settings.json"
    entry_v1 = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 60}
    entry_v2 = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 120}

    _json_merge.merge_hook(settings_path, "Stop", entry_v1, _match_devmemory)
    _json_merge.merge_hook(settings_path, "Stop", entry_v2, _match_devmemory)

    data = json.loads(settings_path.read_text())
    matching = [h for h in data["hooks"]["Stop"][0]["hooks"] if _match_devmemory(h)]
    assert matching == [entry_v2]


def test_merge_hook_raises_clear_error_on_invalid_existing_json(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not valid json")
    entry = {"type": "command", "command": "devmemory-cmd", "args": [], "timeout": 60}

    with pytest.raises(ValueError):
        _json_merge.merge_hook(settings_path, "Stop", entry, _match_devmemory)

    assert settings_path.read_text() == "{not valid json"
