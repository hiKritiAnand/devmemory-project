# devmemory

Persistent, agent-agnostic memory of *why* a codebase changed, plus a
code-structure knowledge graph of *what* exists — for when there's no prior
decision history to query yet. Free, open source (MIT), and distributed
directly from this GitHub repository — no PyPI account or paid tier needed.

**Who it's for:** anyone working on a codebase with one or more AI coding
tools (Claude Code, Cursor, or a plain terminal) who wants decisions and
code structure to survive across sessions and across tools, instead of
living only in one tool's private conversation memory.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/hiKritiAnand/devmemory-project/main/install.sh \
  -o /tmp/devmemory-install.sh
bash /tmp/devmemory-install.sh
```

Supports Linux and macOS (x86_64/arm64); see [docs/INSTALL.md](docs/INSTALL.md)
for what the installer does step by step, upgrading/uninstalling, installing
a specific version/commit, and manual `pip install` (works anywhere Python
3.10–3.12 does, including Windows).

## Quick start

```bash
cd /path/to/existing-project
devmemory init
devmemory scan
devmemory status
devmemory explain "explain the authentication flow"
```

Full walkthrough: [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## The problem

Switching tools (Claude Code today, Cursor tomorrow) loses context: each
tool's conversation memory is sandboxed to itself. And even within one
tool, re-explaining the history of a function every time you touch it
burns tokens you shouldn't need to spend.

## The approach

- **No raw transcripts.** `devmemory` stores structured decision records —
  what changed, why, what was rejected, which file/symbol it applies to,
  when, and now also its *status* (proposed/accepted/rejected/deprecated/
  superseded) and provenance — not conversation logs.
- **Plain files in the repo**, not a tool's private memory. A SQLite index
  (`.devmemory/memory.db`) backs fast queries; every record is also
  mirrored as Markdown under `.devmemory/records/` so it's human-readable,
  grep-able, and git-mergeable even without the CLI.
- **Keyed by file + symbol**, so a query scoped to the function you're
  editing only returns relevant history instead of the whole project log.
- **One core engine, three integration surfaces.** The CLI, the MCP
  server, and the per-tool instruction-file adapters are all thin layers
  over the same `storage.py`/`knowledge.py`/`retrieval.py` functions — no
  graph or retrieval logic is reimplemented per integration.
- **"Zero-token" is honest only for `log`.** `devmemory log --since <date>`
  reads straight from SQLite with no LLM call. Anything that synthesizes a
  narrative answer from the results costs at least one LLM call.
- **Recording is automatic, via two complementary mechanisms** (see below):
  a tool-agnostic git commit hook, and a free in-session nudge inside Claude
  Code.
- **Deterministic first, LLM selectively.** File discovery, parsing, graph
  traversal, conflict detection, and token accounting are all plain code.
  The only place an LLM is invoked is the git-hook commit-extraction path,
  and (opt-in only) conflict arbitration.

## Developing on devmemory itself

```bash
git clone https://github.com/hiKritiAnand/devmemory-project.git
cd devmemory-project
pip install -e ".[dev,mcp]"
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for code conventions and test setup.

## Full command reference

```bash
# One-time setup in a project
devmemory init
devmemory install claude    # writes/updates a block in CLAUDE.md, a Claude Code
                            # Stop-hook, and registers the MCP server in .mcp.json
devmemory install cursor    # writes .cursor/rules/devmemory.mdc and .cursor/mcp.json
devmemory install git       # installs a post-commit hook (see "Automatic recording")

# Introducing devmemory into an existing, already-mature repo
devmemory onboard --dry-run     # see what would be indexed first
devmemory onboard                # discover & index README/AGENTS.md/CLAUDE.md/
                                  # cursor rules/copilot instructions/ADRs/
                                  # OpenAPI specs/migration dirs, link to graph nodes

# Record a decision after making one
devmemory record \
  --file src/billing/invoice.py --symbol calculate_total \
  --summary "Switched total calc to decimal.Decimal" \
  --why "float rounding caused off-by-a-cent invoices" \
  --rejected "Tried rounding at display time only; didn't fix stored totals" \
  --tags billing,bugfix \
  --status accepted            # proposed|accepted|rejected|deprecated|superseded

# Before editing a function, pull its scoped history
devmemory query --file src/billing/invoice.py --symbol calculate_total

# Find a decision by what it's about, not where it lives
devmemory query --text "payment retries"
devmemory explain "payment retries"          # + any onboarded project context mentioning it
devmemory memory query "payment retries"     # decisions only

# "What did I do yesterday" — free, no LLM call
devmemory log --since 2026-10-15

# Repository identity, schema version, graph/decision counts at a glance
devmemory status

# Environment/installation health check (Python version, FTS5, git, MCP extra, graph scanned)
devmemory doctor

devmemory --version
devmemory --help
```

`explain`/`memory query` are keyword search (SQLite FTS5, same engine as
`query --text`) over recorded decisions and onboarded context — not
natural-language understanding. They're meant as a quick first pass before
a precise `query --file --symbol` lookup, not a chat interface.

All commands accept `--path <project-root>` (default: current directory).
Existing stores self-heal on every connection; run `devmemory migrate`
explicitly if you want to apply a pending schema change without touching
anything else first.

## Decision status, supersession, and conflicts

A decision isn't just inserted once and left alone. It has a `status`
(`proposed`/`accepted`/`rejected`/`deprecated`/`superseded`), can carry
`--alternatives`/`--consequences`/`--scope`/`--owner`, and changing it is
always append-only — nothing is ever deleted, only superseded:

```bash
devmemory set-status <id> deprecated --note "no longer true after the rewrite"
devmemory record --file ... --summary "New approach" --supersedes <old-id>
devmemory history --decision-id <id>     # full status/supersession audit trail
```

Recording a decision automatically checks for conflicts with other *active*
(proposed/accepted) decisions covering the same file/symbol/scope — two
deterministic rules, no LLM call by default:

- **`status_overlap`** (high confidence): two active decisions on the same
  target, full stop — worth a look even if the text doesn't obviously clash.
- **`keyword_negation`** (medium confidence): one decision's text negates
  ("not"/"never"/"avoid"/"don't"/"stop using") a term the other asserts
  plainly, over shared vocabulary. This is a blunt heuristic, not NLP — real
  false positives/negatives are expected.

```bash
devmemory conflicts --file src/billing/invoice.py --symbol calculate_total
devmemory conflicts --file ... --symbol ... --llm-arbitrate   # opt-in: one LLM call,
                                                                 # only for keyword_negation candidates
```

By default a decision is scoped to the current git branch (so work on one
branch doesn't leak into another); `--global` records it project-wide, and
`--all-branches` on `query`/`log`/`context` includes every branch's
decisions.

## Knowledge graph

`devmemory knowledge` is a deliberately bounded structural graph alongside
the decision-record store — code *structure*, cross-referenced with
devmemory's own *why*:

```bash
devmemory knowledge scan                              # parse the repo, update the graph (incremental)
devmemory knowledge stats                              # node/edge counts by kind, no re-parse
devmemory knowledge render                             # whole-repo file/import graph, as Mermaid
devmemory knowledge render --detail symbols --file src/billing/invoice.py   # one file's functions/calls
devmemory knowledge inspect --file src/billing/invoice.py --symbol calculate_total  # metadata + edges + decisions
devmemory knowledge impact --file ... --symbol ... [--max-depth N]         # transitive callers -- what breaks if you change it
devmemory knowledge history --file ... --symbol ...   # prior snapshots, only when the body actually changed
devmemory knowledge renames [--mark-resolved <id>]     # advisory same-body-hash rename/move candidates
```

Nodes are keyed by the same `file`+`symbol` devmemory decisions already use,
so `inspect` and the rendered graph both surface any decisions recorded
against a node. `devmemory install git`'s post-commit hook also runs
`knowledge scan` after every commit.

**Languages:** Python, JavaScript, TypeScript/TSX, Java, C#, Go, PHP, C,
C++, HTML, CSS, and SQL — each a `LanguageSpec` in `knowledge.py` (every
node-type/field convention, including every `EXTENDS`/`IMPLEMENTS`
inheritance-clause extractor below, was verified against real tree-sitter
output before being encoded, not guessed). Import and call resolution are
syntactic best-effort everywhere (no type-checking):

- Python resolves imports by dotted-path matching; JS/TS/C/C++ resolve
  relative imports by path. **Java, C#, Go, and PHP imports are never
  resolved to local files** — recorded as `external`, honestly, rather than
  guessed.
- `class Dog(Animal)` / `class Foo extends Bar implements Baz` style
  inheritance produces `extends`/`implements` edges, same-file resolution
  only — Python/C++ only ever "extends" (no interface concept); C#'s
  class-vs-interface split relies on the near-universal `I<Upper...>`
  naming convention (a class that breaks the convention can be mis-tagged).
- A file matched by `is_test_file()`'s heuristic (directory named
  `test(s)`/`spec(s)`, or a filename like `test_foo.py`/`foo.spec.ts`/
  `FooTest.java`) has its existing same-file `calls` edges additionally
  double-tagged `tests` — the common cross-file "test imports the module
  under test" case isn't captured, since that's a cross-file call this
  module never resolves at all.
- HTML and CSS have no functions/classes/calls — only file-level import
  edges. SQL: `CREATE TABLE` becomes a `table`-kind node; `FROM`/`JOIN`
  becomes a `references` edge, attributed to the file itself when outside
  any function body (most real `.sql` is top-level statements).

### Node history and rename detection

A symbol's body is hashed separately from its name/signature, so a pure
rename (logic unchanged) and a genuine body change are distinguishable:

- `node_history` keeps a snapshot from right before a rescan replaces a
  symbol — only when its body actually changed, never on every scan.
- `node_rename_candidates` flags a same-body-hash match between a symbol
  that disappeared and one that newly appeared this scan — `high`
  confidence for an in-file rename, `low` for a cross-file move. Advisory
  only, never auto-merged into a single identity; trivial duplicate bodies
  (e.g. two empty `__init__`s) will produce noisy low-confidence matches.

### Branch isolation

The code graph itself isn't partitioned per branch (it already reflects
whatever's on disk at scan time); `devmemory knowledge scan`/`stats` print a
one-line, non-blocking warning if the graph includes files scanned under a
different branch than the one currently checked out. `git worktree`
(multiple simultaneous checkouts sharing one `.devmemory/memory.db`) isn't
handled — flagged, not silently broken.

## Retrospective onboarding

`devmemory onboard` discovers existing AI-instruction and architectural
context in a repo devmemory is introduced into *after* it already has
history: README*, `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/`,
`.github/copilot-instructions.md`/`.github/instructions/`, ADR-looking
directories, OpenAPI/Swagger specs, and migration directories (summarized
by filename/revision only, never the full SQL). Each is stored as a
`context_record`, scoped to its own directory (a nested instruction file is
never promoted to project-wide), and linked to a graph node only via an
*exact* match — a known file path appearing in its content, or a symbol
name that's globally unique across the whole graph. An ambiguous name
(defined in more than one file) is never guessed at. Idempotent: running it
twice on an unchanged repo indexes/links nothing new.

## Traversal profiles and context compiler

`devmemory context --file <path> --symbol <name> --profile <name>` walks
the graph using one of eight named, deterministic retrieval policies —
`explanation`, `bug_fix`, `feature`, `refactor`, `change_impact`,
`security_sensitive`, `architecture_exploration`, `historical` — each a
plain config (which edge kinds to follow, how deep, whether to pull in
decisions/context-records/history), not a bespoke algorithm per task. `devmemory
profiles` lists them. `devmemory knowledge impact` is a thin wrapper over
the `change_impact` profile.

Add `--compile --budget <n>` to `query`/`log`/`context` to get a
deduplicated, token-budgeted block instead of a raw list — token counts are
an estimate (chars/4, always labeled as such, not a real provider
tokenizer), truncation is never silent (a visible "N item(s) omitted" marker
is appended, never a mid-item cutoff), and the first item is always
included even if it alone exceeds the budget.

## MCP server

```bash
pip install -e ".[mcp]"
devmemory mcp-server --path /your/project    # stdio transport
```

`devmemory install claude`/`install cursor` already register this in
`.mcp.json`/`.cursor/mcp.json` automatically (non-destructive to any other
already-registered server). Six tools, each a thin adapter over the same
core functions the CLI uses — no logic is reimplemented in the MCP layer:

| Tool | What it does |
|---|---|
| `devmemory_search` | Full-text/exact search over decisions |
| `devmemory_explain` | A symbol's structure + related decisions/context |
| `devmemory_impact` | Transitive callers — what breaks if you change it |
| `devmemory_context` | Task-specific traversal via a named profile |
| `devmemory_history` | Decision status history, or a symbol's prior snapshots |
| `devmemory_record` | Record a decision, surfacing any detected conflicts |

Every tool response goes through the same context compiler as the CLI's
`--compile`, so results are already deduplicated and token-budgeted.
Verified end-to-end with a real MCP stdio client/server handshake (not just
in-process calls) before shipping — launching the server, listing tools,
and calling them, exactly how Claude Code/Cursor would.

## Full-text search

`query --file`/`--symbol` only matches an exact file path and a substring
symbol. `query --text "<words>"` searches `summary`/`why`/`rejected`/`tags`
across every decision, ranked by relevance, using SQLite's built-in FTS5 —
not a vector index (current practice for code-adjacent search favors
lexical/agentic search over embeddings; Claude Code itself dropped a
vector-DB approach for this). Degrades automatically to a plain multi-term
`LIKE` search when this SQLite build lacks FTS5.

## Measuring whether this is actually worth it

`devmemory roi [--since <date>]` reports, per creation-source bucket
(costly git-hook LLM extraction vs. everything else), how many decisions
have ever been queried again, the oldest never-queried record, and —
wherever a caller actually ran output through the context compiler
(`--compile` or an MCP tool call) — real estimated-token-cost evidence, not
a guess. A plain uncompiled read logs no estimate at all (`NULL`, never a
fabricated zero).

## Schema migrations

Every schema change since the original scaffold ships as a numbered,
transactional migration in `migrations.py`, tracked in a `schema_migrations`
table, applied automatically on every connection (so an older `.devmemory/`
self-heals) or explicitly via `devmemory migrate`. Nothing is ever lost: a
migration that fails rolls back completely, and FTS5's indexed columns are
deliberately frozen (new decision fields are never added to it, since FTS5
virtual tables can't be `ALTER`'d).

## Security

- Commit diffs are redacted (AWS keys, PEM blocks, GitHub/Slack tokens,
  JWTs, generic `password=`/`api_key=` assignments) before being sent to the
  LLM in the git-hook extraction path — best-effort, not a guarantee.
- File discovery respects `.gitignore` and an optional
  `.devmemory/ignore.json` (`exclude_dirs`/`exclude_globs`), and rejects a
  symlink that resolves outside the repo root.
- No repository content is ever sent to an external model except the
  commit-diff text in the git-hook path (and, opt-in only, a conflict pair's
  summary/why text for `--llm-arbitrate`) — both shell out to your own
  `claude` CLI, no separate API key.

## Status

**Implemented and tested** (all with real automated test coverage — see
`tests/`, run with `pytest`): schema migrations, secret redaction, decision
status/supersession/provenance/history, conflict detection, the full
relationship taxonomy (`extends`/`implements`/`tests` edges plus
`decision_links` for `SUPERSEDES`/`INTRODUCED_BY`/`DOCUMENTED_BY`), node
history and rename-candidate detection, branch isolation, retrospective
onboarding, 8 traversal profiles, the context compiler, the MCP server (6
tools, verified against a real stdio client), and token-cost ROI reporting.

**Known, documented limitations** (not hidden): call/import resolution is
still syntactic, same-file-only, no type checking; `TESTS` edges miss the
common cross-file case; rename detection is pure body-hash matching (any
incidental edit defeats it, trivial duplicates produce noise); C#'s
extends-vs-implements split is a naming-convention heuristic; the 8
traversal profiles' configs and the default token budget are judgment calls
with no real-usage data behind them yet; there's no formal baseline-vs-
devmemory benchmark/ablation suite, only the ROI token-cost instrumentation
above; `git worktree` isn't handled for branch isolation; an `AGENTS.md`
*install adapter* (writing one, as opposed to reading one during onboarding)
still doesn't exist; `explain`/`memory query` are lexical search dressed up
as a question interface, not real natural-language understanding (stated
above, not hidden in the UX).

**Roadmap / not built yet:** a packaged agent-skill (`skills/setup-devmemory/`)
for one-command setup inside a coding agent session — the CLI/MCP server
work without it, this would just make onboarding a new agent session
faster; Windows support for `install.sh` (works today via WSL or a manual
`pip install`, see [docs/INSTALL.md](docs/INSTALL.md)); `devmemory
clean`/`uninstall`/`config` as dedicated subcommands (today: `rm -rf
.devmemory` and `uv tool uninstall devmemory` cover both, see
[docs/USER_GUIDE.md](docs/USER_GUIDE.md)/[docs/INSTALL.md](docs/INSTALL.md)).

## Supported platforms

Python 3.10–3.12, Linux and macOS (x86_64/arm64) via `install.sh`; any OS
Python itself supports via a manual `pip install` (see
[docs/INSTALL.md](docs/INSTALL.md)).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
