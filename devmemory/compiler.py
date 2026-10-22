"""Lightweight context compiler: turns a list of retrieval items into a
single budgeted, deduplicated text block for agent-facing output (CLI
`--compile` and MCP tool responses) -- deliberately not a full tiered
compiler (the heavier spec option). Token counts are an estimate (a chars/4
heuristic), always labeled as such; no real provider tokenizer is wired in.
"""

from dataclasses import dataclass
from typing import Callable, TypeVar

DEFAULT_BUDGET_TOKENS = 2000

T = TypeVar("T")


def estimate_tokens(text: str) -> int:
    """chars/4 heuristic -- a documented estimate, not an exact count from
    any real tokenizer. Rounds up so a non-empty string never estimates to 0."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass
class CompiledContext:
    text: str
    estimated_tokens: int
    is_estimate: bool
    included_items: int
    omitted_items: int


def compile_context(
    items: list[T],
    budget_tokens: int,
    render_item: Callable[[T], str],
    dedup_key: Callable[[T], object] | None = None,
) -> CompiledContext:
    """Renders `items` (already assumed pre-ranked by the caller) via
    `render_item`, deduplicating by `dedup_key` (defaults to the rendered
    text itself) first, then includes items in order until the next one
    would exceed `budget_tokens` -- the first item is always included
    regardless of its own size (nothing smaller to show instead), but once
    budget is hit, every remaining item is omitted as a block, counted in
    `omitted_items`, with a visible marker appended -- never a silent cutoff,
    and never truncates mid-item.
    """
    seen = set()
    rendered: list[str] = []
    for item in items:
        text = render_item(item)
        key = dedup_key(item) if dedup_key is not None else text
        if key in seen:
            continue
        seen.add(key)
        rendered.append(text)

    included: list[str] = []
    total = 0
    omitted = 0
    for idx, text in enumerate(rendered):
        cost = estimate_tokens(text)
        if included and total + cost > budget_tokens:
            omitted = len(rendered) - idx
            break
        included.append(text)
        total += cost

    body = "\n".join(included)
    if omitted:
        marker = f"\n[... {omitted} item(s) omitted -- call again with a higher budget for more ...]"
        body += marker
        total += estimate_tokens(marker)

    return CompiledContext(
        text=body, estimated_tokens=total, is_estimate=True,
        included_items=len(included), omitted_items=omitted,
    )
