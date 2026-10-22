"""Synthesizes a decision record from a single git commit's diff via an LLM
call. This is the only genuinely LLM-costly path in devmemory (see
CONTEXT.md/README's honesty about `log` being the only free read). Invoked
by the post-commit git hook (devmemory/adapters/githook.py) via
`devmemory extract --commit <ref>`.
"""

import json
import subprocess
import sys
from pathlib import Path

from . import secrets, storage

MAX_DIFF_CHARS = 12000
SELF_REFERENCE_DIR = ".devmemory"

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "worth_recording": {"type": "boolean"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "symbol": {"type": ["string", "null"]},
                    "summary": {"type": "string"},
                    "why": {"type": ["string", "null"]},
                    "rejected": {"type": ["string", "null"]},
                    "tags": {"type": ["string", "null"]},
                },
                "required": ["file", "summary"],
            },
        },
    },
    "required": ["worth_recording", "decisions"],
}

PROMPT_TEMPLATE = """You are extracting structured decision records from a single git commit, \
for a tool called devmemory that gives future AI coding assistants scoped \
context about *why* code changed, not just what changed.

Decide whether this commit contains anything decision-worthy (a non-obvious \
tradeoff, a rejected alternative, a bugfix with a non-obvious root cause, a \
deliberate design choice) as opposed to purely mechanical changes (formatting, \
renames, dependency bumps, generated file updates), which are NOT decision-worthy.

If nothing is decision-worthy, return {{"worth_recording": false, "decisions": []}}.

Otherwise return one decision object per distinct file/symbol-level decision \
(usually just one). `file` is the single most relevant repo-relative path; \
`symbol` is the function/class name if identifiable, else null; `summary` is \
one or two sentences on what changed; `why` explains the motivation/tradeoff, \
else null; `rejected` names a considered/rejected alternative if evident, else \
null; `tags` is a short comma-separated string, else null.

Respond with JSON only, matching the provided schema.

Commit message:
---
{message}
---

Diff{truncation_note}:
---
{diff}
---
"""


def _run_git(root, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError(f"git {' '.join(args)} failed: {e}") from e
    return result.stdout


def get_commit_sha(root, commit_ref: str) -> str:
    return _run_git(root, ["rev-parse", commit_ref]).strip()


def get_commit_message(root, commit_ref: str) -> str:
    return _run_git(root, ["log", "-1", "--pretty=%B", commit_ref]).strip()


def get_commit_author(root, commit_ref: str) -> str:
    return _run_git(root, ["log", "-1", "--pretty=%an", commit_ref]).strip()


def get_changed_paths(root, commit_ref: str) -> list[str]:
    output = _run_git(root, ["show", "--name-only", "--pretty=format:", commit_ref])
    return [line.strip() for line in output.splitlines() if line.strip()]


def filter_self_reference_paths(paths: list[str]) -> list[str]:
    return [
        p
        for p in paths
        if p != SELF_REFERENCE_DIR and not p.startswith(f"{SELF_REFERENCE_DIR}/")
    ]


def get_commit_diff_for_paths(root, commit_ref: str, paths: list[str]) -> str:
    if not paths:
        return ""
    return _run_git(root, ["show", "--no-color", "--unified=3", commit_ref, "--", *paths])


def truncate_diff(diff: str, max_chars: int = MAX_DIFF_CHARS) -> tuple[str, bool]:
    if len(diff) <= max_chars:
        return diff, False
    return diff[:max_chars], True


def build_prompt(message: str, diff: str, truncated: bool) -> str:
    truncation_note = " (truncated — diff exceeded size limit, showing first portion only)" if truncated else ""
    return PROMPT_TEMPLATE.format(message=message, diff=diff, truncation_note=truncation_note)


def _default_command() -> list[str]:
    return [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(EXTRACTION_SCHEMA),
        "--tools", "",
        "--permission-mode", "dontAsk",
    ]


def parse_llm_output(raw_stdout: str) -> dict:
    raw_stdout = raw_stdout.strip()
    if not raw_stdout:
        raise ValueError("LLM extraction produced no output")
    envelope = json.loads(raw_stdout)
    if isinstance(envelope, dict) and "structured_output" in envelope:
        return envelope["structured_output"]
    return envelope


def run_llm(prompt: str, llm_command: list[str] | None = None, timeout: int = 120) -> dict:
    command = llm_command if llm_command is not None else _default_command()
    try:
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"LLM extraction command not found: {command[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"LLM extraction command timed out after {timeout}s") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"LLM extraction command failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return parse_llm_output(result.stdout)


def extract_and_record(
    root,
    commit_ref: str = "HEAD",
    llm_command: list[str] | None = None,
    author: str | None = None,
) -> list[dict]:
    root = Path(root)
    storage.init_store(root)

    changed_paths = filter_self_reference_paths(get_changed_paths(root, commit_ref))
    if not changed_paths:
        return []

    sha = get_commit_sha(root, commit_ref)
    message = get_commit_message(root, commit_ref)
    raw_diff = get_commit_diff_for_paths(root, commit_ref, changed_paths)
    # Redact before truncating, not after: truncation could otherwise cut a
    # secret in half and let the remaining fragment slip past the patterns.
    redacted_diff, redaction_count = secrets.redact(raw_diff)
    if redaction_count:
        print(
            f"devmemory extract: redacted {redaction_count} likely secret(s) from the diff before sending it to the LLM",
            file=sys.stderr,
        )
    diff, truncated = truncate_diff(redacted_diff)
    prompt = build_prompt(message, diff, truncated)

    output = run_llm(prompt, llm_command=llm_command)
    if not output.get("worth_recording"):
        return []

    resolved_author = author or get_commit_author(root, commit_ref)
    records = []
    for decision in output.get("decisions") or []:
        record = storage.add_decision(
            root,
            file=decision["file"],
            summary=decision["summary"],
            symbol=decision.get("symbol"),
            why=decision.get("why"),
            rejected=decision.get("rejected"),
            tags=decision.get("tags"),
            session_ref=f"commit:{sha}",
            author=resolved_author,
        )
        records.append(record)
    return records
