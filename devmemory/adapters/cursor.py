"""Writes/updates a marked devmemory block in .cursor/rules/devmemory.mdc for
Cursor, and registers the devmemory MCP server in .cursor/mcp.json."""

from pathlib import Path

from . import _json_merge
from ._shared import install_block

MARKER_START = "<!-- devmemory:start -->"
MARKER_END = "<!-- devmemory:end -->"

FRONTMATTER = """---
description: devmemory - persistent decision records across AI coding sessions
globs:
alwaysApply: true
---

"""

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

RELATIVE_PATH = Path(".cursor") / "rules" / "devmemory.mdc"
MCP_CONFIG_RELATIVE_PATH = Path(".cursor") / "mcp.json"


def render_block() -> str:
    return f"{MARKER_START}\n{BLOCK_BODY}{MARKER_END}\n"


def install_mcp_server(root) -> Path:
    config_path = Path(root) / MCP_CONFIG_RELATIVE_PATH
    # Cursor has no equivalent of Claude Code's ${CLAUDE_PROJECT_DIR}
    # expansion, so this needs a literal absolute path.
    entry = {"command": "devmemory", "args": ["mcp-server", "--path", str(Path(root).resolve())]}
    return _json_merge.merge_mcp_server(config_path, "devmemory", entry)


def install(root) -> list[Path]:
    path = Path(root) / RELATIVE_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FRONTMATTER, encoding="utf-8")
    rules_path = install_block(path, render_block(), MARKER_START, MARKER_END)
    mcp_path = install_mcp_server(root)
    return [rules_path, mcp_path]
