# Context: devmemory project

Paste this whole file as the first message to Claude Code (or any agent) in
the `devmemory/` project folder to continue work with full background.

**Before trusting anything in this file about current code behavior,
verify against the actual files** — this file is a handoff summary, not a
live state inspector, and a prior version of it was once found to describe
a scaffold that didn't actually exist on disk (see devmemory's own decision
records / the auto-memory note about that incident, if available to you).

## Origin

This started from looking at github.com/Graphify-Labs/graphify (a tool that
turns a codebase into a queryable knowledge graph via a `/graphify` skill).
graphify maps code *structure* — it does not capture *why* decisions were
made during development. devmemory's original scope was purely the latter;
a code-structure knowledge graph (`knowledge.py`) was added later as a
deliberate reversal of that founding "no structure, only why" principle,
once a project with no prior decision history had nothing to query at all.

## The problem being solved

When returning to a feature or fixing something in an existing one, the
developer currently has to either remember the context themselves or
re-explain it to the AI assistant from scratch. Worse when switching tools
(Claude Code yesterday, Cursor today) because each tool's conversation
memory is sandboxed and doesn't transfer.

Goals, in the user's own words (original scaffold) and later confirmed
scope (the 2026-10-22 full build-out, see below):
1. Only need context relevant to the function being edited, not the whole
   project history.
2. Context must be useful regardless of which AI tool is used tomorrow.
3. "What did I do yesterday" with no token cost, where possible.
4. (Added later, explicit user choice over a smaller recommended option)
   build toward the fuller spec of a scoped engineering-memory *platform*:
   decision supersession/conflict detection, retrospective onboarding,
   branch isolation, task-specific retrieval, token budgeting, and an MCP
   server — while deliberately rejecting the parts of that fuller spec that
   didn't fit a single-SQLite-file, single/small-team tool (no vector
   search, no bespoke NL query planner inside devmemory itself, no
   PostgreSQL-ready storage abstraction, no heavyweight benchmark/ablation
   harness).

## Key design decisions already made (with rationale)

- **Don't store raw conversation transcripts.** Structured "decision
  records" instead: what changed, why, what was rejected, file/symbol,
  when, status, provenance.
- **Storage must be plain files in the repo, not a tool's internal
  memory.** SQLite (`.devmemory/memory.db`) as the queryable index, each
  decision *also* mirrored as Markdown under `.devmemory/records/`.
- **Decision records are keyed by file + symbol**, not by date/session.
- **One CLI, thin per-tool adapters, now also one MCP server** — all three
  are adapters over the same core engine (`storage.py`/`knowledge.py`/
  `retrieval.py`), never separate reimplementations.
- **"Zero-token" answers are only honest for metadata queries.** `log`
  reads SQLite directly; anything synthesized costs an LLM call.
- **Deterministic first, LLM selectively** — the only two LLM call sites in
  the whole project are the git-hook commit-extraction path
  (`extract.py`/`run_llm`) and opt-in conflict arbitration
  (`conflicts.py`'s `--llm-arbitrate`). Everything else (parsing, graph
  traversal, conflict detection's default rules, token estimation) is plain
  code.
- **No vector search, deliberately.** Cites "lost in the middle" (Liu et
  al.) and Claude Code's own move away from vector-DB RAG for code.
  SQLite FTS5 (with a LIKE fallback) plus graph traversal is the retrieval
  baseline; revisit only if a concrete retrieval failure actually
  demonstrates FTS5+graph can't solve it.
- **No bespoke NL query planner inside devmemory.** devmemory is always
  called from inside an LLM agent that already does natural-language
  interpretation — a second intent classifier would duplicate that. Instead
  devmemory exposes precise, composable, deterministic primitives (the 8
  traversal profiles in `profiles.py`), which the calling agent composes.
- **Append-only where devmemory is the system of record.** Decisions get a
  full status/supersession/history trail (`decision_history`,
  `decision_links`) because devmemory is their only record. Code nodes get
  a *lighter* version of this (`node_history`, `node_rename_candidates`)
  because git is already the authoritative history of code content.
- **`edges` stays node↔node only.** Every relationship that isn't
  node↔node (decision↔decision SUPERSEDES, decision↔commit INTRODUCED_BY,
  node↔context_record DOCUMENTED_BY) lives in a separate polymorphic
  `decision_links` table instead, mirroring `access_log`'s existing
  `ref_kind`/`ref_id` convention. See `relationships.py`'s registry for the
  full disposition of every spec-named relationship kind (most are
  *derived* from an existing fact, not separately stored).
- **Schema changes are migrations, not more `CREATE TABLE IF NOT EXISTS`.**
  `migrations.py`, tracked in `schema_migrations`, applied on every
  connection. FTS5's indexed columns are deliberately frozen at
  `summary/why/rejected/tags` — new decision fields never go in there, since
  FTS5 virtual tables can't be `ALTER`'d.

## What's already built (verified by real test coverage, not just claimed)

All of the following has passing pytest coverage (`pytest` from the project
root; 320+ tests as of the 2026-10-22 build-out) and was smoke-tested
against devmemory's own real repo (dogfooding) and, for the MCP server, a
real stdio client/server handshake — not just in-process calls.

- **`storage.py`** — decisions (status/supersession/provenance/alternatives
  /consequences/scope/owner), `decision_history` (append-only),
  `decision_links` (polymorphic), `context_records`, branch-scoped
  query/search/log (`all_branches=` param), access logging with optional
  token estimates, Markdown mirror.
- **`migrations.py`** — versioned migration framework; `init_store()`/
  `_connect()` both self-heal.
- **`secrets.py` / `ignore_config.py`** — diff redaction before the LLM
  sees it; symlink-boundary + configurable exclusions for file discovery.
- **`conflicts.py`** — `status_overlap`/`keyword_negation` deterministic
  rules, opt-in LLM arbitration.
- **`relationships.py`** — the full spec-named relationship registry.
- **`knowledge.py`** — tree-sitter graph, 12 languages, content-hash
  incremental rescan, `extends`/`implements`/`tests` edge producers (every
  new per-language extractor verified against real tree-sitter output
  before coding, not guessed — see each `_x_inherit` function's docstring),
  `node_history`/`node_rename_candidates`, branch-staleness advisory.
- **`onboard.py`** — retrospective context discovery/linking, idempotent.
- **`profiles.py` / `retrieval.py`** — 8 named traversal profiles over one
  shared BFS; `knowledge.impact()` is now a thin wrapper, output verified
  byte-identical to its pre-refactor behavior.
- **`compiler.py`** — token-estimate (chars/4) budgeting with visible
  truncation markers, wired into `query`/`log`/`context`'s `--compile`.
- **`mcp_server.py`** — 6 tools (`devmemory_search/explain/impact/context/
  history/record`), optional `mcp` extra, registered automatically in
  `.mcp.json`/`.cursor/mcp.json` by the Claude/Cursor adapters.
- **`git_utils.py`** — `current_branch()`, shared by storage/knowledge.
- **CLI** (`cli.py`) — see README.md for the full command reference; every
  new command/flag is additive, existing output formats are unchanged
  unless a new flag (`--compile`, `--all-branches`, etc.) is passed.

## Explicitly not built yet (genuine remaining gaps, not hidden)

1. **An `AGENTS.md` *install* adapter.** `onboard.py` *reads* an existing
   `AGENTS.md` as context during retrospective onboarding, but there is
   still no `devmemory install agents` that *writes* one, the way
   `install claude`/`install cursor` do for their respective tools.
2. **Cross-file call/reference resolution.** Every `calls`/`references`/
   `tests` edge is same-file-only, syntactic name-matching — no type
   resolution anywhere. This is the single biggest precision gap in the
   whole graph, stated plainly in multiple places rather than hidden.
3. **A formal baseline-vs-devmemory benchmark/ablation suite.** Deliberately
   not built (judged too heavy for a single-developer-scale tool) — `roi`'s
   token-cost instrumentation (Phase 12) is the actual, real evidence
   collected instead, but it is not a controlled baseline comparison.
4. **`git worktree` support for branch isolation.** Multiple simultaneous
   checkouts of the same repo sharing one `.devmemory/memory.db` is
   explicitly out of scope, flagged in code rather than silently broken.
5. **Un-retuned judgment calls.** The 8 traversal profiles' exact
   `edge_kinds`/`max_depth` configs and the default token budget
   (`compiler.DEFAULT_BUDGET_TOKENS`) were chosen with zero real-usage data
   — expect to revisit once this is actually used across a few real tasks.
6. **De-duplication between the two automatic-recording mechanisms**
   (git-hook extraction vs. the Stop-hook nudge) can still occasionally
   both record the same change — a pre-existing gap from the original
   scaffold, not addressed by the 2026-10-22 build-out (conflict detection
   catches *contradictory* decisions, not *duplicate* ones).

## Open questions (mostly resolved; noting what's still genuinely open)

- ~~Should `record` happen automatically?~~ **Resolved (2026-10-16):** yes,
  via both the git-hook and the Stop-hook nudge together.
- ~~Should devmemory capture structure, not just why?~~ **Resolved
  (2026-10-16):** yes, `knowledge.py` — a deliberate reversal of the
  original founding principle.
- ~~How far should decision memory/retrieval/integration go beyond the
  original scaffold?~~ **Resolved (2026-10-22):** the user explicitly chose
  the full build-out (12 phases) over a smaller "targeted hardening"
  option, with MCP prioritized and conflict detection extended to include
  arbitration — see this project's own `devmemory` decision records (once
  any are recorded about this session) and the conversation history for the
  full gap analysis and phase-by-phase plan this was built from.
- **Still open:** whether `.devmemory/` is committed to git or gitignored
  (both automatic-recording mechanisms already guard against
  self-reference either way, so this doesn't block anything — it's a team
  workflow preference, not a technical blocker).
- **Still open:** real-usage retuning of traversal-profile configs and the
  default token budget (see gap 5 above) — needs actual usage, not more
  design work, to resolve.
- ~~Should `.devmemory/` be committed or gitignored?~~ **Resolved
  (2026-10-22), for this project's own repo at least:** `.gitignore` ignores
  `memory.db` and the generated `knowledge/` diagram (binary/regenerable)
  but *not* `records/` (the Markdown mirror) — matching the README's own
  stated reason that mirror exists at all. A project adopting devmemory can
  choose differently; this is the choice this project made for itself.

## Public distribution (2026-10-22)

The project was packaged for free public distribution at
`https://github.com/hiKritiAnand/devmemory-project`: `install.sh`
(`curl | bash`, wraps `uv tool install --from git+...`), new CLI commands
(`--version`, `status`, `doctor`, `explain`, `memory query`, a top-level
`scan` alias for `knowledge scan`), `LICENSE` (MIT), `docs/INSTALL.md`,
`docs/USER_GUIDE.md`, `CONTRIBUTING.md`, and CI
(`.github/workflows/test.yml`: pytest on Linux+macOS, a packaging smoke
test that builds and installs the wheel into a clean venv, `install.sh`
syntax/shellcheck).

**Two real naming collisions surfaced and were resolved by deviating from
the literal pasted spec, not by breaking what already existed:**
1. The spec wanted a bare `devmemory status` as a repo-overview command, but
   `devmemory status <id> <new_status>` already existed (Phase 3's
   decision-status mutation, from the engine build-out the same day).
   Renamed the existing one to `devmemory set-status <id> <new_status>` —
   arguably a clearer name for a mutation anyway — and gave the overview
   role to the now-free `status` name, since nothing had been published
   yet and no real users depended on the old name.
2. The spec wanted `devmemory context explain "<question>"`, but `devmemory
   context --file --symbol --profile` already existed (Phase 9's traversal
   command) and a free-text question doesn't fit that shape. Added a
   separate top-level `devmemory explain "<question>"` instead of
   overloading `context`, and documented the deviation rather than quietly
   matching the spec's exact wording.

`explain`/`memory query` are implemented as honest lexical search
(`storage.search_decisions` + a content scan over `context_records`),
explicitly *not* a natural-language answer engine — consistent with the
"no bespoke NL query planner inside devmemory" principle above. Packaging
was verified by actually building the wheel and installing it into a
throwaway venv outside the source tree (not just trusting `pyproject.toml`
looks right) before committing.
