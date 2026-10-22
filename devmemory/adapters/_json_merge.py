"""Shared helper for merging a single hook-command entry into a JSON settings
file (e.g. Claude Code's .claude/settings.json) without disturbing unrelated
hooks or other top-level settings keys.
"""

import json
from pathlib import Path
from typing import Callable


def merge_hook(settings_path: Path, event: str, entry: dict, match: Callable[[dict], bool]) -> Path:
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8")
        try:
            settings = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{settings_path} contains invalid JSON; fix or remove it before running install"
            ) from e
    else:
        settings = {}

    groups = settings.setdefault("hooks", {}).setdefault(event, [])
    for group in groups:
        group["hooks"] = [h for h in group.get("hooks", []) if not match(h)]
    groups[:] = [g for g in groups if g.get("hooks")]

    if groups:
        groups[0]["hooks"].append(entry)
    else:
        groups.append({"hooks": [entry]})

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return settings_path


def merge_mcp_server(config_path: Path, server_name: str, entry: dict) -> Path:
    """Merge a single `{"mcpServers": {<server_name>: entry}}` server
    registration into an MCP config file (Claude Code's `.mcp.json`,
    Cursor's `.cursor/mcp.json`), without disturbing any other
    already-registered server under a different name.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        raw = config_path.read_text(encoding="utf-8")
        try:
            config = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{config_path} contains invalid JSON; fix or remove it before running install"
            ) from e
    else:
        config = {}

    config.setdefault("mcpServers", {})[server_name] = entry
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config_path
