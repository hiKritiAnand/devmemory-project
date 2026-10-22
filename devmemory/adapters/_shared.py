"""Shared helper for merging a marked devmemory block into a target file.

Used by adapters that write into a file the user/tool may also own content
in (e.g. CLAUDE.md), so installing never clobbers unrelated content and
re-installing updates the block in place instead of duplicating it.
"""

from pathlib import Path


def install_block(path: Path, block: str, marker_start: str, marker_end: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        content = path.read_text(encoding="utf-8")
        if marker_start in content and marker_end in content:
            start = content.index(marker_start)
            end = content.index(marker_end) + len(marker_end)
            content = content[:start] + block.rstrip("\n") + content[end:]
        else:
            prefix = content.rstrip("\n")
            content = f"{prefix}\n\n{block}" if prefix else block
    else:
        content = block

    if not content.endswith("\n"):
        content += "\n"

    path.write_text(content, encoding="utf-8")
    return path
