# Contributing

## Setup

```bash
git clone https://github.com/hiKritiAnand/devmemory-project.git
cd devmemory-project
pip install -e ".[dev,mcp]"
```

## Running tests

```bash
pytest           # full suite
pytest -q        # quiet
pytest tests/test_knowledge.py   # one file
```

All tests must pass before a change is considered done. If a pre-existing
test needs to change because a command's output format changed, say so
explicitly in the PR description — don't weaken an assertion just to make
it pass.

## Code conventions (already evident across the codebase — follow them)

- **Deterministic first, LLM selectively.** The only two places this
  project ever shells out to an LLM are `extract.py` (git-hook commit
  extraction) and `conflicts.py`'s opt-in `--llm-arbitrate`. Everything
  else — parsing, graph traversal, conflict detection's default rules,
  token estimation — is plain code. A new feature should default to
  deterministic logic; reach for an LLM call only when the task genuinely
  requires judgment a human would also need to make, and make it opt-in
  where practical.
- **No vector/semantic search, no bespoke NL query planner inside
  devmemory.** Both were deliberately rejected (see CONTEXT.md) — lexical
  search (SQLite FTS5, with a LIKE fallback) plus graph traversal is the
  retrieval baseline, and devmemory exposes composable deterministic
  primitives for the calling agent to use, rather than trying to understand
  free text itself.
- **Verify a new tree-sitter extractor against real parser output before
  encoding it.** Every per-language fact in `knowledge.py` (field names,
  node types) was checked against an actual parse tree, not guessed — see
  any `_x_inherit`/`_x_import_target` function's docstring for the pattern.
  If you add language support, dump a real parse tree for your test
  snippets first (a few lines of `tree_sitter_language_pack.get_parser(...)`
  + a recursive print of `node.type`/`field_name_for_child` is enough).
- **State honest limitations in code, not just docs.** A best-effort rule
  (same-file-only call resolution, a naming-convention heuristic, a
  chars/4 token estimate) gets a comment explaining what it does and
  doesn't cover, right where it's implemented — see `knowledge.py`'s module
  docstring and almost any `_csharp_inherit`-style function for the pattern.
- **No comments that restate the code.** A comment explains a non-obvious
  constraint, a rejected alternative, or a subtle invariant — not what a
  well-named function already says.
- **Additive schema changes only.** Any change to `storage.py`'s/
  `knowledge.py`'s SQLite schema is a new entry in `migrations.py`'s
  `MIGRATIONS` list (never edit an already-shipped migration), applied
  transactionally, never destructive to existing data.
- **One core engine, multiple thin integrations.** The CLI, the MCP server,
  and the per-tool adapters (`adapters/claude.py`, `adapters/cursor.py`)
  are all adapters over the same `storage.py`/`knowledge.py`/`retrieval.py`
  functions. Don't reimplement graph or retrieval logic inside a new
  integration surface.

## Pull requests

- Keep changes focused — a bug fix doesn't need unrelated refactoring
  bundled in.
- Add or update tests for anything you change; this codebase treats "no
  test coverage" as "not actually verified."
- Run the full `pytest` suite before opening the PR, and mention in the
  description which commands you ran manually (if any) to confirm the
  change works beyond the test suite.
