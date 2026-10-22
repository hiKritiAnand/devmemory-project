"""Installs a post-commit git hook that triggers LLM-based decision
extraction (see devmemory/extract.py) and a knowledge-graph rescan (see
devmemory/knowledge.py) for every commit, regardless of which tool (or no
tool) produced it.
"""

import stat
import subprocess
from pathlib import Path

from ._shared import install_block

MARKER_START = "# devmemory:start"
MARKER_END = "# devmemory:end"

BLOCK_BODY = '''# devmemory: extract a decision record from this commit via an LLM call,
# and refresh the code-structure knowledge graph.
# Runs detached so `git commit` returns immediately; failures here never
# block the commit. Logs: .devmemory/extract.log
if command -v devmemory >/dev/null 2>&1; then
  ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
  if [ -n "$ROOT" ]; then
    mkdir -p "$ROOT/.devmemory"
    nohup sh -c 'devmemory extract --path "$1" --commit HEAD; devmemory knowledge scan --path "$1"' \\
      _ "$ROOT" >> "$ROOT/.devmemory/extract.log" 2>&1 < /dev/null &
  fi
fi
'''


def render_block() -> str:
    return f"{MARKER_START}\n{BLOCK_BODY}{MARKER_END}\n"


def _resolve_hooks_dir(root) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"Not a git repository (no .git found at {root})") from e

    git_common_dir = Path(result.stdout.strip())
    if not git_common_dir.is_absolute():
        git_common_dir = (Path(root) / git_common_dir).resolve()
    return git_common_dir / "hooks"


def install(root) -> Path:
    hooks_dir = _resolve_hooks_dir(Path(root))
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"

    if not hook_path.exists():
        hook_path.write_text("#!/bin/sh\n", encoding="utf-8")
    else:
        existing = hook_path.read_text(encoding="utf-8")
        if not existing.startswith("#!"):
            hook_path.write_text("#!/bin/sh\n\n" + existing, encoding="utf-8")

    install_block(hook_path, render_block(), MARKER_START, MARKER_END)
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook_path
