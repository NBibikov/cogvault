# Changelog

## 0.10.0 — 2026-10-05 — the vector channel could only see a third of memory

- **Chunks are fitted to the model's token window.** `paraphrase-multilingual-MiniLM`
  truncates input at **128 tokens**; chunks were packed to 1500 characters
  (~400-500 tokens of Ukrainian). On every multilingual tenant 91-94% of chunks
  were over the limit and only ~35% of stored text was visible to the vector
  channel — the rest was reachable by exact-keyword BM25 alone. Chunks are now
  split (lines → sentences → words) until each fits the window minus the doc
  prefix. Indexes stamp `chunker` in `meta`; a missing or different stamp
  triggers one automatic full rebuild.
- **Embeddings are batched per file** instead of one ONNX run per chunk.
- **`intfloat/multilingual-e5-small` supported** (registered as a fastembed
  custom model; 384-d, 512-token window), with automatic `query: `/`passage: `
  prefixes for the e5 family (`query_prefix`/`doc_prefix` override them).
  End-to-end hybrid eval over three live tenants, description and card-tail
  queries, hit@1 averaged: old 0.655 → MiniLM+fit 0.796 → **e5-small, 700-char
  chunks 0.855** (hit@5 ~0.86 → ~0.975).
- **Temporal decay uses the file's live age.** `chunks.age_days` froze at index
  time, so cards untouched since the last rebuild decayed on a different clock
  from freshly indexed ones (18 days of skew fleet-wide).
- **Migration backfills are no longer logged as card edits.** The v3 backfill
  logged 1572 phantom `update` events — 88% of all edits the log had recorded.
- **Recalls carry a `cold` flag**; `analyze` splits model-load latency (~530 ms
  p50) from warm search latency (~15 ms).
- **MCP server warms the index in a background thread**, so a full rebuild on
  boot no longer blocks `initialize` past the client's timeout.
- **Write-path hardening:** an unclosed leading `---` no longer crashes
  `cogvault_record`; frontmatter `name:` is slugified before becoming a filename
  (`../` could escape the tenant); cards are created with `O_EXCL`, so concurrent
  same-title records cannot overwrite each other; `cogvault_recall` validates
  `query` and clamps `limit`.
- **Long paragraphs are split at whitespace**, not mid-word.

## 0.9.0 — 2026-09-01 — embedding-backend drift, honest metrics

The headline bug: **recall on every multilingual tenant had been silently
degrading for weeks**, and none of the existing metrics could show it.

- **Pinned the fastembed model cache.** `TextEmbedding` was constructed without
  `cache_dir`, so fastembed unpacked its ONNX weights into `$TMPDIR`
  (`/var/folders/...` on macOS). When the OS purged that, indexing and recall
  died with `NoSuchFile: [ONNXRuntimeError] Load model from /var/folders/...` —
  four such failures were sitting in the query log (tenant-a, tenant-b).
  Weights now live under `~/.cache/fastembed` (override:
  `$FASTEMBED_CACHE_PATH`), next to the indexes they belong to.
- **Detect embedding-BACKEND drift, not just model drift.** `_model_mismatch()`
  compared `model` + `dim`, which cannot catch a library that changes what a
  model *outputs*. fastembed 0.6 switched MiniLM to mean pooling and stopped
  L2-normalizing the multilingual model: same name, same 384 dims, different
  vector space. A fleet audit found **35-50% of stored vectors on every
  multilingual tenant diverging from freshly embedded text** (cos as low as
  0.198), with stored norms spanning 2.07-3.06 *within one database* while fresh
  queries came out at norm ~5.3. vec0 ranks by L2, so those queries were ranked
  largely by magnitude rather than meaning. Indexes now stamp
  `backend = fastembed/<version>` into `meta` and treat a change as a mismatch.
  A missing key means "pre-0.9.0, unknown" and does not trigger a surprise
  re-embed — rebuild such tenants once with `reindex(full=True)` after clearing
  `emb_cache`.
- **`dist` on every search result.** The RRF `score` is a rank reciprocal capped
  at `2/rrf_k`: across 616 logged recalls its entire range was 0.016-0.033,
  identical for a correct hit and for gibberish. Results (and the query log's
  `top_dist` / `dists`) now carry the raw vector distance — the only logged value
  that tracks relevance. It is comparable only *within* one tenant+backend.
- **`analyze` stops reporting two misleading numbers.** "avg top score" (an
  average of rank reciprocals) is replaced by per-tenant `dist` p50/p90 plus a
  **weakest-hits** list — each tenant's own worst decile, i.e. the queries that
  returned something but probably not the right thing. "no-hit rate" is relabeled
  "empty results" and annotated: a hybrid search returns the top-k of its
  candidate pool, so it is ~always 0 and never meant "recall is healthy".
- **`cogvault_record` validates `content`.** A missing or blank field raised
  `KeyError` deep inside `_write_card`, surfacing as an opaque `-32000
  "'content'"` — the agent believed it had saved a memory that was never
  written (one silent loss on tenant-d). Now a `-32602` naming the field.
- **Write-side telemetry now covers the path agents actually use.** `log_record`
  fired only from the MCP `cogvault_record` tool, but almost nobody writes that
  way: agents and the `/remember` skill write markdown files and then run
  `cogvault index`. The log therefore showed 616 recalls against 24 records and
  reported five busy tenants as read-only. `reindex()` — the one place that sees
  both paths — now logs a `record` event per touched card with
  `op` = `create` / `update` / `delete`. Three details keep the metric honest:
  events are emitted only AFTER the COMMIT (a rollback must not claim writes
  that never landed), a `full=True` re-embed logs nothing (otherwise a 572-card
  tenant reports 572 fresh facts), and the MCP path no longer logs separately
  (its own reindex covers it — logging both double-counted every tool write).
  `analyze` splits `new cards` (creates, per tenant) from `card edits`, so a
  tenant rewriting `project_state.md` daily can't drown out the real signal.
- **Model weights no longer live under `$XDG_CACHE_HOME`.** That variable is
  redirected per test run and per sandbox to isolate *indexes*; tying the ~300 MB
  ONNX download to it made every isolated run re-fetch the weights. Three
  concurrent test processes each spent 25s+ downloading the same files, which is
  what had been intermittently blowing the concurrency test's timeout. Indexes
  belong to a tenant, weights belong to the machine. Suite: 35s and flaky → 2.9s
  stable.
- **Silenced fastembed's mean-pooling warning** after verifying (post-rebuild)
  that stored and fresh vectors match exactly; it fired on every CLI call and
  buried real output in subagent scrollback.

## 0.8.2 — 2026-07-28 — failure visibility & evergreen standing rules

- **`error` events in the query log.** Until now the log recorded only what
  succeeded, so a broken tenant was indistinguishable from a forgetful agent:
  an MCP recall that raised returned a JSON-RPC error to the caller and left no
  trace, and a CLI recall died with a traceback into a subagent's scrollback.
  Both paths now append one `{"event": "error", tenant, op, error, message}`
  line via `obs.log_error()` before propagating. Failures are never swallowed —
  the CLI still re-raises, the MCP server still answers `-32000`.
- **`analyze` surfaces failures.** An `errors` line (with a per-exception-type
  breakdown and the last five failures) prints above the no-hit list, and
  `errors` / `errors_by_type` appear in `--json`. Errors rank above no-hits
  because a no-hit is a memory gap while an error is broken plumbing.
- **`feedback_*` cards are decay-exempt.** The default `evergreen_re` covered
  `MEMORY`/`INDEX`/`reference_`/`architecture` but not `feedback_` — so on a
  tenant with `half_life_days` set, standing rules ("never build unasked", "no
  Android commits") sank below fresh session notes purely because nobody had
  edited the file in months. On the tenant-a tenant this exempted 200 more
  chunks. Rebuild the index to recompute the flag on existing tenants.

## 0.8.1 — 2026-07-08 — write-side telemetry & log hygiene

- **`record` events in the query log.** `cogvault_record` (MCP) now logs one
  `{"event": "record", tenant, file, chars}` line next to recalls; `analyze`
  prints a `records` line with a per-tenant breakdown (and `records` /
  `records_by_tenant` in `--json`). Read-only tenants — agents that recall but
  never record — are now visible at a glance.
- **No-hit nudge.** An empty recall is the exact moment an agent knows a memory
  card is missing, so both the MCP "No matching memories found." response and
  the CLI (stderr, keeping `--json` stdout clean) now suggest recording a card
  after solving.
- **Query-log rotation.** The append-only log rotates to `.1` past 5 MB (one
  generation kept) instead of growing unbounded across the fleet's lifetime.

## 0.8.0 — 2026-07-02 — card types, wiki-links, schema v2

- **Card-type filter.** The YAML frontmatter `type` of each card (both the flat
  `type: reference` and the nested `metadata:\n  type: reference` shapes) is parsed
  at index time — no YAML dependency, same minimal-parser philosophy as the TOML
  reader — and stored per chunk. Filter recall with CLI `search --type <t>`, MCP
  `cogvault_recall` `type` param, or `Vault.search(card_type=...)`. Every hit now
  carries a `type` field (None for cards without frontmatter — legacy cards degrade
  gracefully). Filtering post-filters 4×-deepened candidate pools (vec0 `MATCH`
  can't take a joined WHERE); a very rare type buried below that depth can be
  missed — documented limitation.
- **Wiki-link awareness.** `[[slug]]` / `[[slug|alias]]` / `[[slug#section]]`
  targets are indexed into a `links` table; the TOP search result gains a
  `related` list of linked card filenames that actually exist in the index
  (ghost links dropped; exact stem match — case-variant links won't resolve).
  CLI prints a `related:` line; MCP appends a `Related:` line to the first block.
- **Schema v2 with cheap automatic migration.** Existing v1 index DBs are upgraded
  in place on first connect (race-safe `BEGIN IMMEDIATE` + re-check): `type`
  column + `links` table added, and every `files` mtime is invalidated (keys kept
  — deletion tracking must survive the migration, or files removed between the
  last v1 index and the backfill would linger as searchable orphans) so the next
  reindex backfills both — every embedding comes from `emb_cache`, so
  **no re-embed**. Until that reindex runs, types are NULL (the MCP server
  reindexes on boot).
- Query log records the `type` filter when used; regression test added for the
  0.7.1 `parent/basename` tenant label fix.
- Changelog repaired: entries for 0.5.0, 0.6.0 and 0.7.1 below were missing, and
  ordering is now strictly descending.

## 0.7.1 — 2026-07-02 — correctness & concurrency fixes

- **Per-(process, model) embedder registry.** A single global embedder silently
  embedded other-model tenants with the wrong model and persisted poisoned vectors
  into `emb_cache` — permanent silent recall degradation.
- **Model-mismatch wipe order.** FTS5 `delete-all` now runs BEFORE the content table
  is cleared (the old order left stale postings misattributed to reused rowids);
  a full rebuild clears ALL derived rows, not just paths known to `files`;
  `emb_cache` survives the wipe (it is model-keyed, switching back stays cheap).
- **Reads don't stall behind reindex.** Embeddings are computed OUTSIDE the write
  transaction; schema DDL + `user_version` stamp happen only on db creation, so the
  read path takes no write lock.
- **Single-flight heal** per db + `Vault.join_heal()`; CLI search waits out a
  triggered heal and retries once instead of leaving a broken index behind.
- **Loud stderr warnings**: unparsable/unusable `.cogvault.toml`, model mismatch on
  reindex (full re-embed) and on search (unreliable vector ranking).
- **Tenant db key**: path realpath'd + casefolded on macOS/Windows — case-variant
  spellings of one dir no longer maintain two flapping indexes.
- **Query-log tenant label** is now `parent/basename` (fleet tenants all named
  `memory` were indistinguishable); `analyze` matches legacy labels too.
- **MCP**: `serverInfo` reports the real version, JSON-RPC notifications are not
  answered, `ping` supported; `record` cards get frontmatter and collision-safe
  filenames.
- Oversized paragraphs hard-split at `chunk_chars` so their tails stay visible to
  the vector channel; minimal-TOML parser respects `#` inside quotes.
- Tests: `conftest` isolates the cache dir and query log from the real `~/.cache`
  (tests had leaked 249 MB of dbs); regressions for the embedder registry, FTS wipe
  order, and orphan purge.

## 0.7.0 — 2026-06-28 — per-tenant config

- **`.cogvault.toml` per-tenant config.** A tenant can now declare its embedding
  model (and other `Config` fields) in a `.cogvault.toml` at the tenant root, so the
  model travels WITH the data instead of relying on every caller exporting
  `COGVAULT_MODEL`. A bare `cogvault search --tenant <dir>` now picks up the right
  model automatically. Without this, omitting the env var silently fell back to the
  multilingual default, and the model mismatch triggered a full re-embed wipe that
  flapped against the MCP tool's index.
  - Precedence: explicit `Config(...)` / `--model` / `$COGVAULT_MODEL` > `.cogvault.toml` > defaults.
  - Allowlisted keys only (model, dim, chunk/pool sizes, decay, mmr, recursive,
    strip_frontmatter, ignore_globs); `db_dir` is intentionally NOT settable from the file.
  - TOML via stdlib `tomllib` (3.11+) with a minimal flat-key fallback for 3.10.
  - cogvault never *writes* the file — read-only if present.
  - New `--model` CLI flag for one-off overrides. New public `apply_tenant_config()`.

## 0.6.0 — 2026-06-28 — index folder trees (Obsidian vaults & knowledge bases)

Opt-in multi-source support: a tenant can be a nested vault, not just a flat
directory. Flat agent-memory tenants are unaffected (all new options default off).

- `Config`: `recursive`, `strip_frontmatter`, `ignore_globs`.
- Recursive walk keys files by path RELATIVE to the tenant, so same-named notes in
  different folders no longer collide on basename.
- `strip_frontmatter` drops a leading YAML `--- … ---` block before chunking so its
  keys don't pollute embeddings; `ignore_globs` skips paths (e.g. `.obsidian/*`,
  `Templates/*`); the recursive walk prunes dotfile dirs.
- CLI: `--recursive` / `--strip-frontmatter` / `--ignore` on `index|search|mcp`.
- Validated on a real ~3,500-note Obsidian vault: 9,544 chunks, first index ~216 s,
  warm recall 9-10 ms.

## 0.5.0 — 2026-06-28 — first public release

Fleet-grade local memory over plain Markdown, published to GitHub (MIT).

- Atomic model-mismatch guard + rebuild inside one transaction (no more malformed
  vec0 shadow tables after a crash mid-rebuild).
- Resilient self-healing search: a corrupt index returns empty and triggers one
  background rebuild instead of raising into the agent.
- Packaging for fleet install (`pip install -e`), entry point `cogvault`.

## 0.4.0 — 2026-06-27 — observability

- **Query log.** Every `recall` appends one JSONL line to
  `~/.cache/cogvault/query-log.jsonl` (`COGVAULT_LOG` to relocate, `off` to disable):
  timestamp, tenant, query, result count, top score, scores, latency, empty flag.
  Best-effort — never breaks a recall.
- **`cogvault analyze`.** Reads the log into an effectiveness report: recall count,
  no-hit rate, latency p50/p95, average top score, per-tenant breakdown, and recent
  no-hit queries (the actionable "memory is missing this" signal). `--json` for scripts,
  `--tenant` to scope.

## 0.3.1 — 2026-06-27 — concurrency fix (runtime stress test)

A runtime stress test (8 parallel processes hammering one tenant) caught a bug that
two static audits missed:

- **Reindex is now atomic** (`BEGIN IMMEDIATE`). Two processes reindexing the *same*
  tenant concurrently previously raced into a `vec_chunks` rowid UNIQUE-constraint
  crash; they now serialize cleanly via the busy-timeout. Verified: 3 writers +
  5 readers, 0 lock errors, 0 empty reads. Regression-locked by
  `test_concurrent_writers_no_collision`.
- Verified at scale: 419 files / 445 chunks index in ~3 s, search p50 ≈ 5 ms,
  recall holds among 400 distractors. Empty / 1 MB / binary files don't crash.

## 0.3.0 — 2026-06-27 — pre-release contract hardening

Fixing one-way-door decisions before any adoption locks them in.

- **Multilingual by default.** `DEFAULT_MODEL` is now
  `paraphrase-multilingual-MiniLM-L12-v2` (384-d). `bge-small-en-v1.5` returns a
  *negative* relevance margin on Cyrillic queries — broken for non-English memory.
  The new default works for mixed-language fleets; pick your model via
  `COGVAULT_MODEL` / `Config(model=...)` — see the table in the README. (Documented
  trade-off: the multilingual default is less sharp on English; English-only fleets
  should set `bge-small-en-v1.5`.)
- **Schema version + dim guard.** `PRAGMA user_version` and the embedding model **and
  dimension** are recorded in `meta`; a mismatch auto-rebuilds the derived tables
  instead of crashing or silently mixing dimensions.
- **Stable chunk ids.** `search()` now returns a deterministic `id` = `sha(path|hash)`
  that survives reindexes, so external systems can reference a memory safely.
- **Index moved out of the markdown dir.** The `.db` now lives under
  `~/.cache/cogvault/` (override with `Config(db_dir=...)`), so it can never be
  accidentally git-committed next to your notes.

## 0.2.0 — 2026-06-27 — fleet-hardening

Production-correctness pass before fleet rollout (audited with an adversarial review).

- **Incremental indexing.** `reindex()` now only re-reads files whose `mtime`
  changed; unchanged files are skipped, deleted files are purged. `reindex(full=True)`
  forces a clean rebuild.
- **Concurrency-safe.** WAL journal mode + `busy_timeout` — concurrent agents can
  read while another writes. No more full-wipe: a reader during a reindex never sees
  an empty database. (Covered by `test_concurrent_read_during_reindex`.)
- **Correct provenance.** `UNIQUE(path, hash)` — an identical chunk in two files keeps
  both file paths instead of collapsing to the first.
- **FTS5 hardening.** Each query term is double-quoted (FTS keywords like `OR`/`NEAR`
  and punctuation can't break parsing) and English stopwords are dropped — fixing
  silent crashes and BM25 noise. **Recall improved: hit@1 87% → 93%, MRR 0.883 → 0.933.**
- **Full chunks returned** (`text`), not truncated to 280 chars — agents get the
  context they need. `Config.snippet_chars` controls the short preview.
- **Model/dimension guard.** The index records its embedding model; switching models
  auto-wipes incompatible cached vectors instead of crashing.

## 0.1.0 — 2026-06-27

Initial release.

- Markdown-as-source-of-truth memory over a rebuildable SQLite index.
- Hybrid retrieval: `sqlite-vec` (semantic) + FTS5 (BM25), fused with RRF.
- In-process embeddings via FastEmbed (`bge-small-en-v1.5`, 384-d) — no server,
  no API key, no cloud.
- Content-hash embedding cache: re-indexing only re-embeds changed chunks.
- Optional temporal decay (evergreen files exempt) and MMR diversity.
- Multi-tenant: one process serves many isolated memory namespaces by directory.
- stdio MCP server exposing `cogvault_recall` and `cogvault_record`.
- CLI: `index`, `search`, `mcp`, `stats`.
