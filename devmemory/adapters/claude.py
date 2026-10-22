"""Writes/updates a marked devmemory block in CLAUDE.md for Claude Code, and
registers a Stop-hook that nudges the agent to record a decision before
finishing a turn in which it left unrecorded changes.
"""

from pathlib import Path

from . import _json_merge
from ._shared import install_block

MARKER_START = "<!-- devmemory:start -->"
MARKER_END = "<!-- devmemory:end -->"

SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.json"
# Claude Code's "command"-type hooks spawn `command` directly (posix_spawn,
# no shell), with `args` as a separate argv array — NOT a single shell
# command line parsed by a shell. `command` must therefore be a bare
# executable, with every argument (including the project path, which may
# itself contain spaces) as its own `args` element, so it's passed through
# as one argv entry with no quoting needed.
STOP_HOOK_COMMAND = "devmemory"
STOP_HOOK_ARGS = ["check-session", "--path", "${CLAUDE_PROJECT_DIR}"]

BLOCK_BODY = """## devmemory

This project uses `devmemory` to persist decision records that capture
*why* code changed, not just what changed, so context survives across
sessions and across tools (Claude Code, Cursor, etc).

Before editing an existing function, check for relevant prior history:

    devmemory query --file <path> --symbol <name>

After making a change that involved a non-obvious tradeoff, a rejected
alternative, or a decision future-you will want to remember, record it:

    devmemory record --file <path> --symbol <name> \\
        --summary "<what changed>" --why "<why>" --rejected "<what was rejected>"

To see what changed recently, with no LLM call required:

    devmemory log --since <date>
"""


def render_block() -> str:
    return f"{MARKER_START}\n{BLOCK_BODY}{MARKER_END}\n"


def _is_devmemory_stop_hook(entry: dict) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("type") == "command"
        and entry.get("command") == "devmemory"
        and "check-session" in (entry.get("args") or [])
    )


def install_stop_hook(root) -> Path:
    settings_path = Path(root) / SETTINGS_RELATIVE_PATH
    entry = {"type": "command", "command": STOP_HOOK_COMMAND, "args": STOP_HOOK_ARGS, "timeout": 60}
    return _json_merge.merge_hook(settings_path, "Stop", entry, _is_devmemory_stop_hook)


MCP_CONFIG_RELATIVE_PATH = Path(".mcp.json")


def install_mcp_server(root) -> Path:
    config_path = Path(root) / MCP_CONFIG_RELATIVE_PATH
    entry = {"command": "devmemory", "args": ["mcp-server", "--path", "${CLAUDE_PROJECT_DIR}"]}
    return _json_merge.merge_mcp_server(config_path, "devmemory", entry)


def install(root, target_filename: str = "CLAUDE.md") -> list[Path]:
    md_path = Path(root) / target_filename
    install_block(md_path, render_block(), MARKER_START, MARKER_END)
    settings_path = install_stop_hook(root)
    mcp_path = install_mcp_server(root)
    return [md_path, settings_path, mcp_path]
