"""Checks a Claude Code session's dirty files for a missing devmemory record
and decides whether the Stop hook should block. Kept separate from stdin/
stdout handling (which lives in cli.py) for testability.
"""

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import storage

NUDGE_STATE_RELATIVE_PATH = Path(".devmemory") / "stop_nudge_state.json"
SELF_REFERENCE_DIR = ".devmemory"

NUDGE_REASON_TEMPLATE = """You modified the following file(s) without recording a devmemory \
decision for any of them: {files}. Before finishing, either:
- call `devmemory record --file <path> [--symbol <name>] --summary "..." --why "..." \
[--rejected "..."]` for each change worth remembering, or
- if there is genuinely nothing decision-worthy here (e.g. formatting, a typo \
fix, generated output), no action is needed — this check will not ask again \
for this exact set of changes."""


def _is_self_reference(path: str) -> bool:
    return path == SELF_REFERENCE_DIR or path.startswith(f"{SELF_REFERENCE_DIR}/")


def get_dirty_files(root) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []

    paths = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.add(path)
    return sorted(p for p in paths if not _is_self_reference(p))


def _hash_paths(paths: list[str]) -> str:
    joined = "\n".join(sorted(paths))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _nudge_state_path(root) -> Path:
    return Path(root) / NUDGE_STATE_RELATIVE_PATH


def _load_nudge_state(root) -> dict | None:
    path = _nudge_state_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None


def _save_nudge_state(root, state: dict) -> None:
    path = _nudge_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _clear_nudge_state(root) -> None:
    path = _nudge_state_path(root)
    if path.exists():
        path.unlink()


def has_matching_record(root, dirty_files: list[str], since: str, session_id: str | None) -> bool:
    for f in dirty_files:
        for record in storage.query_decisions(root, file=f):
            if session_id and record.get("session_ref") == session_id:
                return True
            if record["created_at"] >= since:
                return True
    return False


def check_session(root, session_id: str | None, stop_hook_active: bool) -> str | None:
    if stop_hook_active:
        return None

    if not storage.get_db_path(root).exists():
        return None

    dirty_files = get_dirty_files(root)
    if not dirty_files:
        _clear_nudge_state(root)
        return None

    new_hash = _hash_paths(dirty_files)
    state = _load_nudge_state(root)
    if state and state.get("hash") == new_hash:
        first_seen_at = state["first_seen_at"]
        already_nudged = state.get("nudged", False)
    else:
        first_seen_at = datetime.now(timezone.utc).isoformat()
        already_nudged = False
        state = {"hash": new_hash, "first_seen_at": first_seen_at, "nudged": False}
        _save_nudge_state(root, state)

    if has_matching_record(root, dirty_files, since=first_seen_at, session_id=session_id):
        state["nudged"] = False
        _save_nudge_state(root, state)
        return None

    if already_nudged:
        return None

    state["nudged"] = True
    _save_nudge_state(root, state)
    return NUDGE_REASON_TEMPLATE.format(files=", ".join(dirty_files))


def handle_stdin_payload(root, payload: dict) -> dict | None:
    session_id = payload.get("session_id")
    stop_hook_active = bool(payload.get("stop_hook_active", False))
    reason = check_session(root, session_id=session_id, stop_hook_active=stop_hook_active)
    if reason is None:
        return None
    return {"decision": "block", "reason": reason}
