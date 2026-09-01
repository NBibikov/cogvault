import os, time, tempfile, shutil, threading
import pytest
from cogvault.core import Vault, Config, chunk_markdown, strip_frontmatter
import cogvault.core as core


def _concurrent_writer(tenant, ident, q):
    """Module-level (picklable for multiprocessing spawn on macOS)."""
    import sqlite3
    from cogvault.core import Vault as V
    errs = 0
    for op in range(8):
        try:
            with open(os.path.join(tenant, f"w{ident}_{op}.md"), "w") as f:
                f.write(f"writer {ident} op {op} unique payload alpha.")
            V(tenant).reindex()
        except sqlite3.OperationalError:
            errs += 1
    q.put(errs)


@pytest.fixture
def tmpvault():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def test_chunking_packs_paragraphs():
    text = "para one here.\n\npara two here.\n\n" + ("x " * 2000)
    assert len(chunk_markdown(text, 1500)) >= 2


def test_index_and_recall(tmpvault):
    _write(tmpvault, "service.md",
           "Service recovery: rerun the bootstrap script to fetch a fresh access "
           "credential and clear the local cache when the backend stops responding.")
    _write(tmpvault, "gateway.md",
           "The gateway routes every client through a single local entry point on a fixed port.")
    v = Vault(tmpvault)
    stats = v.reindex()
    assert stats["files_total"] == 2 and stats["chunks"] >= 2
    res = v.search("how do I fix the backend when it breaks", k=2)
    assert res and res[0]["file"] == "service.md"


def test_multi_tenant_isolation(tmpvault):
    a = os.path.join(tmpvault, "agentA"); os.makedirs(a)
    b = os.path.join(tmpvault, "agentB"); os.makedirs(b)
    _write(a, "secret.md", "AgentA knows the production database password is hunter2.")
    _write(b, "other.md", "AgentB only knows about cat pictures and weather.")
    va, vb = Vault(a), Vault(b)
    va.reindex(); vb.reindex()
    assert not any("hunter2" in r["snippet"] for r in vb.search("database password", k=3))
    assert any("hunter2" in r["snippet"] for r in va.search("database password", k=3))


def test_content_hash_cache(tmpvault):
    _write(tmpvault, "a.md", "stable content that does not change between reindexes.")
    v = Vault(tmpvault); v.reindex()
    s2 = v.reindex()
    # unchanged file -> skipped entirely (0 reindexed, 0 embedded)
    assert s2["files_reindexed"] == 0 and s2["embedded"] == 0


def test_incremental_only_touches_changed(tmpvault):
    _write(tmpvault, "stable.md", "this file never changes across reindexes ok.")
    _write(tmpvault, "edited.md", "original content here.")
    v = Vault(tmpvault); v.reindex()
    time.sleep(0.01)
    _write(tmpvault, "edited.md", "completely new content after the edit happened.")
    s = v.reindex()
    assert s["files_reindexed"] == 1            # only edited.md, not stable.md
    res = v.search("new content after edit", k=1)
    assert res and res[0]["file"] == "edited.md"


def test_deleted_file_is_purged(tmpvault):
    _write(tmpvault, "keep.md", "keep this around for the long term please.")
    _write(tmpvault, "gone.md", "this unique zebra content will be deleted soon.")
    v = Vault(tmpvault); v.reindex()
    os.remove(os.path.join(tmpvault, "gone.md"))
    v.reindex()
    assert not any("zebra" in r["snippet"] for r in v.search("zebra content", k=5))


def test_provenance_path_per_file(tmpvault):
    """Identical chunk in two files must keep BOTH file paths (UNIQUE(path,hash))."""
    shared = "This exact shared sentence appears verbatim in two different files."
    _write(tmpvault, "alpha.md", shared)
    _write(tmpvault, "beta.md", shared)
    v = Vault(tmpvault); v.reindex()
    con = v._connect()
    paths = {r[0] for r in con.execute("SELECT path FROM chunks WHERE text LIKE '%shared sentence%'")}
    con.close()
    assert paths == {"alpha.md", "beta.md"}, f"lost provenance: {paths}"


def test_fts_special_chars_dont_crash(tmpvault):
    """Queries with quotes/brackets/FTS keywords must not throw — agents search code."""
    _write(tmpvault, "code.md", "The function parseConfig() reads settings from config.yaml at boot.")
    v = Vault(tmpvault); v.reindex()
    for q in ['parseConfig() "config"', "what about OR AND NEAR", 'a[b]c "quote', "settings: boot"]:
        res = v.search(q, k=3)            # must not raise
        assert isinstance(res, list)
    # a meaningful code query still finds the right doc
    assert v.search("parseConfig reads config", k=1)[0]["file"] == "code.md"


def test_full_chunk_returned(tmpvault):
    long = "Decision context. " + ("filler sentence padding the chunk well beyond 280 chars. " * 8)
    _write(tmpvault, "long.md", long)
    v = Vault(tmpvault); v.reindex()
    r = v.search("decision context filler", k=1)[0]
    assert len(r["text"]) > 280            # full chunk, not truncated
    assert r["snippet"] == r["text"]       # default snippet_chars=0 => full


def test_multilingual_recall(tmpvault):
    """Ukrainian query must find Ukrainian content via the VECTOR channel
    (not just FTS). The default model must be multilingual."""
    _write(tmpvault, "service_uk.md",
           "Відновлення сервісу: запустити скрипт ініціалізації щоб отримати "
           "свіжий доступ і очистити кеш коли бекенд перестає відповідати.")
    _write(tmpvault, "gateway_uk.md",
           "Шлюз маршрутизує всі запити клієнтів через єдину локальну точку входу.")
    v = Vault(tmpvault); v.reindex()
    # paraphrased Ukrainian query with minimal shared words → relies on semantics
    res = v.search("як полагодити сервіс коли він зламався", k=1)
    assert res and res[0]["file"] == "service_uk.md", \
        f"multilingual vector recall failed: {[r['file'] for r in res]}"


def test_model_switch_rebuilds(tmpvault):
    """Switching the embedding model must auto-wipe the incompatible cache, not crash."""
    from cogvault.core import Config
    _write(tmpvault, "x.md", "content for model switch guard test here.")
    Vault(tmpvault, Config(model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")).reindex()
    # switch to bge-en (also 384d) — guard should wipe + rebuild cleanly
    v2 = Vault(tmpvault, Config(model="BAAI/bge-small-en-v1.5"))
    stats = v2.reindex()
    assert stats["chunks"] >= 1
    assert v2.search("model switch content", k=1)


def test_stable_chunk_id_across_reindex(tmpvault):
    """The returned chunk id must be deterministic — stable across reindexes
    and unrelated edits, so external systems can reference it."""
    _write(tmpvault, "a.md", "first stable fact about the alpha subsystem here.")
    _write(tmpvault, "b.md", "unrelated beta fact that will be edited later on.")
    v = Vault(tmpvault); v.reindex()
    id1 = v.search("alpha subsystem fact", k=1)[0]["id"]
    # edit a DIFFERENT file, reindex, re-query — a.md's id must not change
    _write(tmpvault, "b.md", "completely different beta content now changed.")
    v.reindex()
    id2 = v.search("alpha subsystem fact", k=1)[0]["id"]
    assert id1 == id2, "chunk id changed across reindex"
    assert id1 and isinstance(id1, str)


def test_db_outside_tenant_dir(tmpvault):
    """The index DB must NOT be written into the markdown dir (git-leak risk)."""
    _write(tmpvault, "note.md", "some content to index for the location test.")
    v = Vault(tmpvault); v.reindex()
    assert os.path.dirname(v.db_path) != tmpvault, "db inside tenant dir!"
    # no .db / .cogvault.db left in the markdown directory
    leaked = [f for f in os.listdir(tmpvault) if f.endswith(".db") or "cogvault" in f]
    assert not leaked, f"db artifacts leaked into tenant dir: {leaked}"


def test_db_dir_override(tmpvault):
    from cogvault.core import Config
    custom = os.path.join(tmpvault, "_dbs");
    _write(tmpvault, "n.md", "override test content here please.")
    v = Vault(tmpvault, Config(db_dir=custom))
    v.reindex()
    assert v.db_path.startswith(custom)
    assert v.search("override test", k=1)


def test_concurrent_writers_no_collision(tmpvault):
    """Two PROCESSES reindexing the same tenant must serialize, not crash on a
    vec_chunks rowid collision. (Caught by the runtime stress test, not static review.)"""
    import multiprocessing as mp
    for i in range(10):
        _write(tmpvault, f"d{i}.md", f"document {i} distinct topic {i} content here.")
    Vault(tmpvault).reindex()
    q = mp.Queue()
    procs = [mp.Process(target=_concurrent_writer, args=(tmpvault, i, q)) for i in range(3)]
    for p in procs: p.start()
    for p in procs: p.join(timeout=60)
    total = sum(q.get() for _ in procs)
    assert total == 0, f"{total} concurrent reindex errors (rowid collision regressed)"


def test_query_log_written(tmpvault, monkeypatch):
    """Each recall appends one JSONL line with the fields analyze() needs."""
    import json as _json
    logf = os.path.join(tmpvault, "qlog.jsonl")
    monkeypatch.setenv("COGVAULT_LOG", logf)
    _write(tmpvault, "g.md", "Worker service recovery procedure documented here.")
    v = Vault(tmpvault); v.reindex()
    v.search("how to fix the worker", k=2)
    assert os.path.exists(logf)
    rec = _json.loads(open(logf).readline())
    for key in ("ts", "event", "tenant", "query", "n_results", "top_score", "latency_ms"):
        assert key in rec, f"log missing {key}"
    assert rec["event"] == "recall" and rec["query"] == "how to fix the worker"


def test_logging_disabled(tmpvault, monkeypatch):
    monkeypatch.setenv("COGVAULT_LOG", "off")
    _write(tmpvault, "x.md", "content here for disabled-log test.")
    v = Vault(tmpvault); v.reindex()
    v.search("content", k=1)   # must not raise, must not write anywhere
    assert not os.path.exists(os.path.join(tmpvault, "qlog.jsonl"))


def test_db_is_rebuildable(tmpvault):
    _write(tmpvault, "x.md", "rebuildable index test content for recovery.")
    v = Vault(tmpvault); v.reindex()
    os.remove(v.db_path)
    for ext in ("-wal", "-shm"):
        try: os.remove(v.db_path + ext)
        except OSError: pass
    v.reindex(full=True)
    assert v.search("recovery content", k=1)


def test_concurrent_read_during_reindex(tmpvault):
    """Fleet gate: a reader during an incremental reindex must not see an empty DB."""
    for i in range(8):
        _write(tmpvault, f"doc{i}.md", f"document number {i} about distinct topic {i} alpha.")
    v = Vault(tmpvault); v.reindex()
    empties = []

    def reader():
        for _ in range(20):
            r = Vault(tmpvault).search("distinct topic alpha", k=3)
            empties.append(len(r) == 0)

    t = threading.Thread(target=reader); t.start()
    for i in range(3):
        _write(tmpvault, f"doc{i}.md", f"document number {i} updated topic {i} alpha now.")
        v.reindex()
    t.join()
    # incremental reindex never wipes everything, so readers always see results
    assert not any(empties), "reader saw an empty DB mid-reindex"


def test_search_resilient_to_corrupt_index(tmpvault):
    """A degraded/corrupt vec0 index must NOT propagate 'malformed' to the caller;
    search() returns [] and fires a background heal instead of raising."""
    import sqlite3
    _write(tmpvault, "a.md", "alpha content about the production deploy pipeline.")
    v = Vault(tmpvault); v.reindex()
    assert v.search("deploy pipeline", k=1)            # healthy baseline
    # Corrupt the vec0 shadow tables the way an interrupted rebuild would:
    # drop rows from a shadow table so vec0's internal pointers dangle.
    con = sqlite3.connect(v.db_path)
    try:
        con.execute("DELETE FROM vec_chunks_chunks")   # shadow table — now inconsistent
        con.commit()
    finally:
        con.close()
    # Must degrade gracefully, not raise sqlite3.DatabaseError.
    res = v.search("deploy pipeline", k=1)
    assert res == [] or isinstance(res, list)
    # Background heal eventually restores recall (full rebuild from markdown).
    for _ in range(50):
        if v.search("deploy pipeline", k=1):
            break
        time.sleep(0.1)
    assert v.search("deploy pipeline", k=1), "background heal did not restore the index"


def test_model_switch_rebuild_is_atomic(tmpvault, monkeypatch):
    """If a mismatch-triggered rebuild is killed mid-way, the txn must roll back to
    the previous model's index — never leave an empty chunks table + broken vec0."""
    import sqlite3
    from cogvault import core
    _write(tmpvault, "x.md", "content one about the alpha subsystem here.")
    _write(tmpvault, "y.md", "content two about the beta subsystem here.")
    m_old = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    Vault(tmpvault, Config(model=m_old)).reindex()
    before = Vault(tmpvault, Config(model=m_old)).search("alpha subsystem", k=1)
    assert before, "baseline recall under old model failed"

    # Switch model → mismatch path. Make the re-embed blow up mid-rebuild.
    orig = core.Vault._index_file
    calls = {"n": 0}
    def boom(self, con, key, fp, ev_re, prepared=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated kill mid-rebuild")
        return orig(self, con, key, fp, ev_re, prepared)
    monkeypatch.setattr(core.Vault, "_index_file", boom)

    v2 = Vault(tmpvault, Config(model="BAAI/bge-small-en-v1.5"))
    with pytest.raises(RuntimeError):
        v2.reindex()

    # The killed rebuild must have rolled back: meta still says OLD model and the
    # OLD index is intact (not an empty, half-wiped, malformed state).
    con = sqlite3.connect(v2.db_path)
    try:
        meta = {k: val for k, val in con.execute("SELECT key,value FROM meta")}
        n_chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    finally:
        con.close()
    assert meta.get("model") == m_old, "meta was mutated despite rollback"
    assert n_chunks >= 1, "rollback left an empty chunks table (atomicity broken)"
    # And recall under the original model still works — index never went malformed.
    assert Vault(tmpvault, Config(model=m_old)).search("beta subsystem", k=1)


# ---- per-tenant config (.cogvault.toml) -------------------------------------

def test_tenant_config_sets_model(tmpvault):
    """`.cogvault.toml` makes the model travel with the tenant — a bare Vault()
    (no Config, no env) picks it up, so the CLI can't accidentally use the default."""
    _write(tmpvault, ".cogvault.toml", 'model = "BAAI/bge-small-en-v1.5"\n')
    _write(tmpvault, "x.md", "tenant config model selection content here.")
    v = Vault(tmpvault)
    assert v.cfg.model == "BAAI/bge-small-en-v1.5"
    v.reindex()
    con = v._connect()
    meta = {k: val for k, val in con.execute("SELECT key,value FROM meta")}
    con.close()
    assert meta.get("model") == "BAAI/bge-small-en-v1.5"


def test_tenant_config_does_not_override_explicit(tmpvault):
    """An explicit Config(model=...) from the caller wins over the file."""
    _write(tmpvault, ".cogvault.toml", 'model = "BAAI/bge-small-en-v1.5"\n')
    _write(tmpvault, "x.md", "explicit config beats file content here.")
    m = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    v = Vault(tmpvault, Config(model=m))
    assert v.cfg.model == m


def test_tenant_config_env_wins(tmpvault, monkeypatch):
    """$COGVAULT_MODEL wins over the file (explicit operator intent)."""
    monkeypatch.setenv("COGVAULT_MODEL",
                       "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    _write(tmpvault, ".cogvault.toml", 'model = "BAAI/bge-small-en-v1.5"\n')
    _write(tmpvault, "x.md", "env beats file content here.")
    v = Vault(tmpvault)
    assert v.cfg.model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_tenant_config_sets_source_options(tmpvault):
    """Non-model fields (recursive, strip_frontmatter, ignore_globs) also load."""
    _write(tmpvault, ".cogvault.toml",
           'recursive = true\nstrip_frontmatter = true\nignore_globs = ["Templates/*"]\n')
    v = Vault(tmpvault)
    assert v.cfg.recursive is True
    assert v.cfg.strip_frontmatter is True
    assert v.cfg.ignore_globs == ("Templates/*",)


def test_tenant_config_absent_uses_default(tmpvault):
    """No file → built-in default model (no crash, no change in behavior)."""
    from cogvault.core import DEFAULT_MODEL
    _write(tmpvault, "x.md", "no config file default model content.")
    v = Vault(tmpvault)
    assert v.cfg.model == DEFAULT_MODEL


def test_tenant_config_malformed_is_ignored(tmpvault):
    """A broken config must not crash construction — bad keys/values are skipped."""
    from cogvault.core import DEFAULT_MODEL
    _write(tmpvault, ".cogvault.toml", 'model = \ndim = "not-an-int"\nbogus_key = 5\n')
    _write(tmpvault, "x.md", "malformed config resilience content.")
    v = Vault(tmpvault)                     # must not raise
    assert v.cfg.model == DEFAULT_MODEL     # unparseable model line ignored
    assert v.cfg.dim == 384                 # bad int ignored, default kept


def test_tenant_config_not_written_into_tenant(tmpvault):
    """cogvault must never CREATE a config file — it only reads one if present."""
    _write(tmpvault, "x.md", "cogvault never writes config content here.")
    Vault(tmpvault).reindex()
    assert not os.path.exists(os.path.join(tmpvault, ".cogvault.toml"))


# ---- Obsidian / multi-source support (recursive + frontmatter + ignore) ------

def test_strip_frontmatter_unit():
    assert strip_frontmatter("---\ntags: [a]\ncreated: 2026\n---\n\nbody here") == "\nbody here"
    # no frontmatter → untouched
    assert strip_frontmatter("# heading\n\nbody") == "# heading\n\nbody"
    # a --- inside the body (not leading) is NOT treated as frontmatter
    assert strip_frontmatter("intro\n\n---\n\nmore") == "intro\n\n---\n\nmore"


def test_flat_tenant_ignores_subdirs_by_default(tmpvault):
    """Default (non-recursive) must still see only top-level .md — no behavior change."""
    _write(tmpvault, "top.md", "top level note about alpha widgets.")
    sub = os.path.join(tmpvault, "nested"); os.makedirs(sub)
    _write(sub, "deep.md", "deep nested note about beta gadgets.")
    v = Vault(tmpvault); stats = v.reindex()
    assert stats["files_total"] == 1                  # nested/deep.md NOT indexed
    files = {r["file"] for r in v.search("beta gadgets", k=3)}
    assert "deep.md" not in files and not any("nested" in f for f in files)


def test_recursive_indexes_subdirs(tmpvault):
    """recursive=True walks the tree (e.g. an Obsidian vault layout)."""
    os.makedirs(os.path.join(tmpvault, "01-Projects", "X"))
    os.makedirs(os.path.join(tmpvault, "02-Areas"))
    _write(os.path.join(tmpvault, "01-Projects", "X"), "plan.md",
           "Project X deployment plan: ship the alpha widget pipeline in Q3.")
    _write(os.path.join(tmpvault, "02-Areas"), "health.md",
           "Area note about beta gadget maintenance schedules.")
    v = Vault(tmpvault, Config(recursive=True))
    stats = v.reindex()
    assert stats["files_total"] == 2
    res = v.search("alpha widget deployment", k=1)
    assert res and res[0]["file"] == os.path.join("01-Projects", "X", "plan.md")


def test_recursive_same_basename_no_collision(tmpvault):
    """Two notes named the same in different folders must both be indexed
    (the relative-path key prevents the basename collision a flat key would cause)."""
    a = os.path.join(tmpvault, "projA"); os.makedirs(a)
    b = os.path.join(tmpvault, "projB"); os.makedirs(b)
    _write(a, "Tasks.md", "ProjA tasks: migrate the alpha database to the new cluster.")
    _write(b, "Tasks.md", "ProjB tasks: redesign the beta onboarding flow for users.")
    v = Vault(tmpvault, Config(recursive=True))
    stats = v.reindex()
    assert stats["files_total"] == 2, "basename collision dropped a file"
    files = {r["file"] for r in v.search("alpha database migration", k=3)}
    assert os.path.join("projA", "Tasks.md") in files


def test_recursive_strip_frontmatter_and_ignore(tmpvault):
    """Frontmatter is stripped from embeddings; ignore_globs skip whole subtrees."""
    os.makedirs(os.path.join(tmpvault, "notes"))
    os.makedirs(os.path.join(tmpvault, "Templates"))
    _write(os.path.join(tmpvault, "notes"), "n.md",
           "---\ntags: [zzzcanary]\ncreated: 2026-01-01\n---\n\nThe gamma protocol resets the cache nightly.")
    _write(os.path.join(tmpvault, "Templates"), "tmpl.md",
           "{{title}} template scaffold gamma placeholder should be ignored.")
    v = Vault(tmpvault, Config(recursive=True, strip_frontmatter=True,
                               ignore_globs=("Templates/*",)))
    stats = v.reindex()
    assert stats["files_total"] == 1                  # Templates/ skipped
    # frontmatter token 'zzzcanary' must NOT be retrievable (it was stripped)
    res = v.search("gamma protocol cache", k=1)
    assert res and "zzzcanary" not in res[0]["text"]

# ---- 0.7.1 regression tests ---------------------------------------------------

def test_embedder_is_per_model():
    """One process touching tenants pinned to different models must get different
    embedders — a single global here poisoned emb_cache with wrong-model vectors."""
    from cogvault.core import _embedder
    a = _embedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    b = _embedder("BAAI/bge-small-en-v1.5")
    assert a is not b
    assert a is _embedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")


def test_model_switch_purges_fts_postings(tmpvault):
    """The mismatch wipe must clear the FTS5 inverted index BEFORE the content
    table: wiping chunks first left stale term→rowid postings that misattributed
    old terms to whatever new chunk reused the rowid."""
    import sqlite3
    _write(tmpvault, "old.md", "the zebraword sanctuary migration notes.")
    Vault(tmpvault, Config(model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")).reindex()
    os.remove(os.path.join(tmpvault, "old.md"))
    _write(tmpvault, "new.md", "completely unrelated gadget assembly instructions.")
    v2 = Vault(tmpvault, Config(model="BAAI/bge-small-en-v1.5"))
    v2.reindex()   # mismatch → full wipe + rebuild
    con = sqlite3.connect(v2.db_path)
    try:
        rows = con.execute(
            "SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH 'zebraword'").fetchall()
    finally:
        con.close()
    assert rows == [], "stale FTS postings survived the mismatch wipe"


def test_full_reindex_purges_orphaned_chunks(tmpvault):
    """reindex(full=True) must clear ALL derived rows, not only paths present in
    the `files` table — orphans (older-version leftovers) must not survive the
    rebuild that _heal_async relies on."""
    import sqlite3
    _write(tmpvault, "x.md", "legit content about delta pipelines.")
    v = Vault(tmpvault, Config(model="BAAI/bge-small-en-v1.5"))
    v.reindex()
    con = sqlite3.connect(v.db_path)
    con.execute("INSERT INTO chunks(cid,path,hash,text) VALUES('orph','ghost.md','h','orphan text')")
    con.commit(); con.close()
    v.reindex(full=True)
    con = sqlite3.connect(v.db_path)
    try:
        ghosts = con.execute("SELECT count(*) FROM chunks WHERE path='ghost.md'").fetchone()[0]
    finally:
        con.close()
    assert ghosts == 0, "orphaned chunk survived a full rebuild"


# ---- 0.8.0: frontmatter type filter + wiki-links ------------------------------

def test_parse_frontmatter_type_unit():
    from cogvault.core import parse_frontmatter_type as pft
    assert pft("---\nname: x\nmetadata:\n  type: user\n---\nbody") == "user"
    assert pft("---\ntype: feedback\n---\nbody") == "feedback"
    assert pft('---\ntype: "Project"\n---\nbody') == "project"
    assert pft("---\r\ntype: reference\r\n---\r\nbody") == "reference"
    # flat wins over nested (deterministic)
    assert pft("---\ntype: flatwin\nmetadata:\n  type: nested\n---\n") == "flatwin"
    assert pft("no frontmatter at all") is None
    assert pft("---\nname: x\n---\nbody") is None            # no type key
    assert pft("---\ntype: null\n---\n") is None
    assert pft("--- broken frontmatter\ntype: x\n") is None  # malformed block
    # a body-level "type:" line after the frontmatter must be ignored
    assert pft("---\nname: x\n---\n\ntype: bogus\n") is None


def test_parse_wikilinks_unit():
    from cogvault.core import parse_wikilinks as pw
    text = "See [[b]] and [[c|alias]] plus [[d#section]] and [[b]] again. [not][links]"
    assert pw(text) == ["b", "c", "d"]
    assert pw("no links here") == []


def test_type_filter_search(tmpvault):
    _write(tmpvault, "who.md",
           "---\nname: who\nmetadata:\n  type: user\n---\n\n"
           "The operator prefers concise answers and works on trading systems.")
    _write(tmpvault, "state.md",
           "---\nname: state\ntype: project\n---\n\n"
           "The operator dashboard project is half shipped, next milestone is auth.")
    _write(tmpvault, "legacy.md",
           "A legacy note about the operator with no frontmatter at all.")
    v = Vault(tmpvault)
    v.reindex()
    allres = v.search("operator", k=5)
    assert {r["file"] for r in allres} == {"who.md", "state.md", "legacy.md"}
    assert all("type" in r for r in allres)
    only_user = v.search("operator", k=5, card_type="user")
    assert {r["file"] for r in only_user} == {"who.md"}
    assert only_user[0]["type"] == "user"


def test_missing_frontmatter_tolerated(tmpvault):
    _write(tmpvault, "vestige-x.md",
           "---\nvestige_id: abc\nnode_type: fact\n---\n\n"
           "Orphan card about the deployment pipeline secret rotation.")
    v = Vault(tmpvault)
    v.reindex()
    hit = v.search("deployment pipeline rotation", k=2)
    assert hit and hit[0]["file"] == "vestige-x.md" and hit[0]["type"] is None
    assert v.search("deployment pipeline rotation", k=2, card_type="user") == []


def test_v1_db_migration_no_reembed(tmpvault):
    import sqlite3
    _write(tmpvault, "reference_a.md",
           "---\nname: a\nmetadata:\n  type: reference\n---\n\n"
           "Durable fact linking to [[reference_b]] for context.")
    _write(tmpvault, "reference_b.md",
           "---\nname: b\nmetadata:\n  type: reference\n---\n\nAnother durable fact.")
    v = Vault(tmpvault)
    v.reindex()
    # Simulate a v1 database: drop the 0.8.0 additions and stamp the old version.
    con = sqlite3.connect(v.db_path)
    con.execute("DROP TABLE links")
    con.execute("DROP INDEX idx_chunks_type")
    con.execute("ALTER TABLE chunks DROP COLUMN type")
    con.execute("PRAGMA user_version=1")
    con.execute("DELETE FROM meta WHERE key='schema'")
    con.commit(); con.close()
    # A fresh Vault must migrate on connect (search must not raise) …
    v2 = Vault(tmpvault)
    v2.search("durable fact", k=1)
    # … and the backfill reindex must be embed-free (everything from emb_cache).
    stats = v2.reindex()
    assert stats["embedded"] == 0 and stats["files_reindexed"] == 2
    con = sqlite3.connect(v2.db_path)
    assert con.execute("PRAGMA user_version").fetchone()[0] >= 2
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE type='reference'").fetchone()[0] > 0
    assert con.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 1
    con.close()


def test_related_top_result(tmpvault):
    _write(tmpvault, "a.md",
           "The quarterly billing incident playbook references [[b]] and [[ghost]].")
    _write(tmpvault, "b.md", "Escalation contacts for the billing system.")
    v = Vault(tmpvault)
    v.reindex()
    res = v.search("quarterly billing incident playbook", k=2)
    assert res[0]["file"] == "a.md"
    assert res[0].get("related") == ["b.md"]          # ghost link dropped
    others = [r for r in res[1:]]
    assert all("related" not in r for r in others)


def test_related_updates_on_edit(tmpvault):
    _write(tmpvault, "a.md", "Payment retry logic notes, see [[b]].")
    _write(tmpvault, "b.md", "Retry backoff table.")
    v = Vault(tmpvault)
    v.reindex()
    assert v.search("payment retry logic", k=1)[0].get("related") == ["b.md"]
    time.sleep(0.02)
    _write(tmpvault, "a.md", "Payment retry logic notes, link removed.")
    os.utime(os.path.join(tmpvault, "a.md"))
    v.reindex()
    assert "related" not in v.search("payment retry logic", k=1)[0]


def test_query_log_tenant_label_disambiguated(tmp_path, monkeypatch):
    """Locks in the 0.7.1 fix: every agent tenant is ~/Agents/<name>/memory, so a
    bare-basename label collapsed the whole fleet into one 'memory' bucket."""
    import json
    tenant = tmp_path / "agent-a" / "memory"
    tenant.mkdir(parents=True)
    (tenant / "note.md").write_text("A small note about socket timeouts.")
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("COGVAULT_LOG", str(log))
    v = Vault(str(tenant))
    v.reindex()
    v.search("socket timeouts", k=1, card_type="reference")
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["tenant"] == "agent-a/memory"
    assert rec["type"] == "reference"                 # filter is logged too


def test_v1_migration_preserves_deletion_tracking(tmpvault):
    """A file deleted between the last v1 index and the post-migration backfill
    must still be purged. The first migration draft cleared `files` entirely,
    which erased the deletion baseline and left the dead file's chunks matching
    searches forever."""
    import sqlite3
    _write(tmpvault, "keep.md", "Durable note about connection pooling limits.")
    _write(tmpvault, "dead.md", "Obsolete note about floppy disk rotation speeds.")
    v = Vault(tmpvault)
    v.reindex()
    # Simulate v1 on-disk state…
    con = sqlite3.connect(v.db_path)
    con.execute("DROP TABLE links")
    con.execute("DROP INDEX idx_chunks_type")
    con.execute("ALTER TABLE chunks DROP COLUMN type")
    con.execute("PRAGMA user_version=1")
    con.commit(); con.close()
    # …then the file disappears BEFORE anything reconnects (rename/cleanup window).
    os.remove(os.path.join(tmpvault, "dead.md"))
    v2 = Vault(tmpvault)
    v2.reindex()                       # migration + backfill in one go
    hits = v2.search("floppy disk rotation", k=3)
    assert all(h["file"] != "dead.md" for h in hits)         # orphan purged
    assert v2.search("connection pooling limits", k=1)       # survivor intact
    con = sqlite3.connect(v2.db_path)
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE path='dead.md'").fetchone()[0] == 0
    con.close()


def test_record_event_logged(tmpvault, monkeypatch):
    """cogvault_record writes a `record` line so analyze can pair reads with writes."""
    import json
    from cogvault.mcp_server import MCPServer
    logf = os.path.join(tmpvault, "qlog.jsonl")
    monkeypatch.setenv("COGVAULT_LOG", logf)
    srv = MCPServer(tmpvault, Config())
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "cogvault_record",
                                  "arguments": {"content": "Deploys need the staging flag set.",
                                                "title": "deploy staging flag"}}})
    assert "error" not in resp
    recs = [json.loads(l) for l in open(logf)]
    rec = [r for r in recs if r["event"] == "record"][-1]
    assert rec["file"].startswith("card-")
    assert rec["chars"] > 0
    assert rec["tenant"].count("/") == 1          # parent/basename label, not bare


def test_empty_recall_nudges_record(tmpvault):
    """A no-hit recall is when the agent knows a card is missing — the MCP
    response must point at cogvault_record, not dead-end with 'not found'."""
    from cogvault.mcp_server import MCPServer
    srv = MCPServer(tmpvault, Config())
    srv.vault.reindex()
    resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "cogvault_recall",
                                  "arguments": {"query": "anything at all"}}})
    text = resp["result"]["content"][0]["text"]
    assert "cogvault_record" in text


def test_query_log_rotates(tmpvault, monkeypatch):
    """Past MAX_LOG_BYTES the log rolls to .1 instead of growing unbounded."""
    from cogvault import obs
    logf = os.path.join(tmpvault, "qlog.jsonl")
    monkeypatch.setenv("COGVAULT_LOG", logf)
    monkeypatch.setattr(obs, "MAX_LOG_BYTES", 100)
    for i in range(5):
        obs.log_record(tmpvault, f"card-{i}.md", "x" * 120)
    assert os.path.exists(logf + ".1")
    assert os.path.getsize(logf) <= 300           # rotated, not accumulated


def test_mcp_failure_is_logged_and_still_raised(tmpvault, monkeypatch):
    """A failing recall must leave a trace: without the `error` event a broken
    tenant is indistinguishable from an agent that simply forgot something.
    The caller must STILL get its JSON-RPC error — logging is not swallowing."""
    import json
    from cogvault.mcp_server import MCPServer
    logf = os.path.join(tmpvault, "qlog.jsonl")
    monkeypatch.setenv("COGVAULT_LOG", logf)
    srv = MCPServer(tmpvault, Config())

    def boom(*a, **kw):
        raise RuntimeError("index is toast")
    monkeypatch.setattr(srv.vault, "search", boom)

    resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "cogvault_recall",
                                  "arguments": {"query": "deploy flag"}}})
    assert resp["error"]["code"] == -32000         # caller still sees the failure
    errs = [json.loads(l) for l in open(logf) if '"error"' in l]
    err = [e for e in errs if e.get("event") == "error"][-1]
    assert err["op"] == "cogvault_recall"
    assert err["error"] == "RuntimeError"
    assert err["query"] == "deploy flag"           # the query is the repro handle


def test_standing_rule_cards_are_decay_exempt(tmpvault):
    """feedback_/reference_/MEMORY cards are standing rules, not dated state:
    an old `feedback_never_build_unasked` must not sink below a fresh session
    note just because nobody edited the file. Dated project_ cards DO decay."""
    import re
    ev = re.compile(Config().evergreen_re)
    for name in ("MEMORY.md", "INDEX.md", "reference_release_flow.md",
                 "feedback_never_build_unasked.md", "architecture_overview.md"):
        assert ev.match(name), f"{name} should be evergreen"
    for name in ("project_daily_20260728.md", "2026-07-28.md",
                 "card-20260728-120000-something.md"):
        assert not ev.match(name), f"{name} should decay"


def test_obs_logging_never_raises_into_callers(tmpvault, monkeypatch):
    """Observability is best-effort: an unwritable log must not take down a
    recall. A broken log directory is an annoyance, not an outage."""
    from cogvault import obs
    monkeypatch.setenv("COGVAULT_LOG", "/nonexistent-root-dir/nope/log.jsonl")
    obs.log_error(tmpvault, "search", ValueError("x"))   # must not raise
    obs.log_record(tmpvault, "card.md", "content")


def test_backend_change_is_a_mismatch(tmpvault, monkeypatch):
    """A library that changes what a model OUTPUTS must invalidate the index.

    fastembed 0.6 switched MiniLM to mean pooling and stopped normalizing the
    multilingual model: same model name, same dim, incompatible vectors. Before
    0.9.0 that was undetectable, and 35-50% of stored vectors on every
    multilingual tenant silently drifted out of the query's space.
    """
    _write(tmpvault, "a.md", "The gateway routes clients through one local entry point.")
    v = Vault(tmpvault)
    v.reindex()
    con = v._connect()
    assert dict(con.execute("SELECT key,value FROM meta"))["backend"] == core.embed_backend()
    assert not v._model_mismatch(con)          # same backend → no rebuild
    monkeypatch.setattr(core, "embed_backend", lambda: "fastembed/99.0.0")
    assert v._model_mismatch(con)              # bumped backend → rebuild
    con.close()


def test_legacy_index_without_backend_is_not_a_mismatch(tmpvault):
    """An index written before 0.9.0 has no `backend` key. That means "unknown",
    not "wrong" — it must NOT trigger a surprise full re-embed on first connect."""
    _write(tmpvault, "a.md", "The gateway routes clients through one local entry point.")
    v = Vault(tmpvault)
    v.reindex()
    con = v._connect()
    con.execute("DELETE FROM meta WHERE key='backend'")
    con.commit()
    assert not v._model_mismatch(con)
    con.close()


def test_search_results_carry_vector_distance(tmpvault):
    """RRF `score` is a rank reciprocal and says nothing about relevance; `dist`
    is the value callers and the query log can actually judge a hit by."""
    _write(tmpvault, "gateway.md",
           "The gateway routes every client through a single local entry point on a fixed port.")
    v = Vault(tmpvault)
    v.reindex()
    res = v.search("how does the gateway route clients", k=1)
    assert res and res[0]["dist"] is not None
    assert res[0]["dist"] >= 0
