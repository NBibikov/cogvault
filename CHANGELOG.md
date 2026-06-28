# Changelog

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
