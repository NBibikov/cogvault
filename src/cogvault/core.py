"""
cogvault.core — fleet-grade local memory over plain Markdown.

Source of truth = Markdown files under a tenant directory. The SQLite index
(.cogvault.db) is a rebuildable derived cache. One embedding model is loaded
once per process and shared across every tenant. No cloud, no Docker, no LLM.

Pipeline:  files -> markdown-aware chunks -> content-hash dedup/cache ->
           FastEmbed vectors (sqlite-vec) + FTS5 (BM25) -> RRF fusion ->
           temporal decay -> MMR diversity.
"""
from __future__ import annotations
import os, re, struct, hashlib, sqlite3, glob, math, time, threading
from dataclasses import dataclass, field

import sqlite_vec

# Multilingual by default: agent memory is rarely English-only. This model embeds
# Cyrillic, CJK, etc. correctly. Same 384-d as bge-small-en (no vec-table change),
# no query/passage prefixes required. Override via Config.model for English-only fleets.
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384-d
DIM = 384
SCHEMA_VERSION = 1
# Index lives OUTSIDE the tenant's markdown dir by default, so it can never be
# accidentally git-committed next to the source files. Override via Config.db_dir.
DEFAULT_DB_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "cogvault")


def _default_model() -> str:
    return os.environ.get("COGVAULT_MODEL", DEFAULT_MODEL)


@dataclass
class Config:
    model: str = field(default_factory=_default_model)
    dim: int = DIM
    chunk_chars: int = 1500          # ~380 tokens
    rrf_k: int = 60
    vec_pool: int = 30               # candidates pulled per channel
    fts_pool: int = 30
    half_life_days: float = 0.0      # 0 = decay OFF; e.g. 30 for fast-moving
    mmr_lambda: float = 0.7          # 1=pure relevance, 0=pure diversity
    snippet_chars: int = 0           # 0 = return full chunk (agents have big context)
    db_dir: str = ""                 # "" = ~/.cache/cogvault; set to a dir to override
    evergreen_re: str = r"^(MEMORY|INDEX|.*reference_|.*architecture).*"


# ---- one shared embedder per process ---------------------------------------
_EMBEDDER = None
def _embedder(model: str):
    global _EMBEDDER
    if _EMBEDDER is None:
        from fastembed import TextEmbedding
        _EMBEDDER = TextEmbedding(model_name=model)
    return _EMBEDDER

def embed(texts: list[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    return [list(v) for v in _embedder(model).embed(texts)]


# ---- helpers ----------------------------------------------------------------
def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def _pack(v) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

def chunk_markdown(text: str, chunk_chars: int) -> list[str]:
    """Split on blank lines, then pack paragraphs up to chunk_chars."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) < chunk_chars:
            cur = (cur + "\n\n" + p).strip()
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    return chunks or ([text.strip()] if text.strip() else [])

def _file_age_days(path: str) -> float:
    try:
        return max(0.0, (time.time() - os.path.getmtime(path)) / 86400.0)
    except OSError:
        return 0.0

# Common English stopwords — OR-ing these against FTS5 matches nearly every row,
# blowing up IO and drowning the BM25 signal. Strip them from the keyword channel.
_STOPWORDS = frozenset("""
a an and are as at be by do does for from how i if in into is it its me my no not
of on or our so that the their them then there these they this to was we what when
where which who why will with you your
""".split())

def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression: each meaningful term double-quoted
    (so FTS5 keywords like OR/NEAR/AND and punctuation can't break parsing),
    stopwords dropped. Returns '' when nothing meaningful remains."""
    terms = [t for t in re.findall(r"\w+", query.lower())
             if t not in _STOPWORDS and len(t) > 1]
    if not terms:                                  # all-stopword query: keep originals
        terms = re.findall(r"\w+", query.lower())
    # double-quote each term; FTS5 treats a quoted token as a literal phrase
    return " OR ".join(f'"{t}"' for t in terms)


# ---- the store --------------------------------------------------------------
class Vault:
    """One Vault == one tenant directory of Markdown files."""

    def __init__(self, tenant_dir: str, config: Config | None = None):
        self.dir = os.path.abspath(os.path.expanduser(tenant_dir))
        self.cfg = config or Config()
        os.makedirs(self.dir, exist_ok=True)
        # Index DB lives outside the markdown dir (no accidental git commit).
        # Filename is derived from the tenant path so tenants never collide.
        db_dir = os.path.expanduser(self.cfg.db_dir) if self.cfg.db_dir else DEFAULT_DB_DIR
        os.makedirs(db_dir, exist_ok=True)
        tag = hashlib.sha256(self.dir.encode()).hexdigest()[:16]
        name = os.path.basename(self.dir) or "root"
        self.db_path = os.path.join(db_dir, f"{name}-{tag}.db")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        # WAL: concurrent readers don't block on a writer (fleet-safe).
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")  # for future migrations
        con.executescript(f"""
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY,            -- internal rowid (vec0/fts5 need INT)
                cid TEXT,                          -- STABLE id = sha(path|hash), safe to reference
                path TEXT, hash TEXT,
                text TEXT, age_days REAL DEFAULT 0, evergreen INTEGER DEFAULT 0,
                UNIQUE(path, hash));
            CREATE INDEX IF NOT EXISTS idx_chunks_cid ON chunks(cid);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL);
            CREATE TABLE IF NOT EXISTS emb_cache(
                hash TEXT, model TEXT, vec BLOB, PRIMARY KEY(hash, model));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{self.cfg.dim}]);
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
                text, content='chunks', content_rowid='id');
        """)
        # First-run only: stamp meta. The model/dim *mismatch* wipe is NOT done
        # here — see _model_mismatch()/reindex(). Doing the wipe in this separate
        # transaction (then rebuilding in a later one) left a window where a crash
        # between wipe-commit and rebuild-commit produced an empty chunks table +
        # inconsistent vec0 shadow tables → "database disk image is malformed" on
        # the next search(). The wipe now happens INSIDE reindex()'s atomic txn.
        prev = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
        if not prev:
            want = {"model": self.cfg.model, "dim": str(self.cfg.dim),
                    "schema": str(SCHEMA_VERSION)}
            con.executemany("INSERT INTO meta(key,value) VALUES(?,?)", want.items())
        con.commit()
        return con

    def _model_mismatch(self, con) -> bool:
        """True if the index was built with a different model/dim than the current
        config — its cached vectors are incompatible and must be rebuilt."""
        prev = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
        if not prev:
            return False
        return (prev.get("model") != self.cfg.model
                or prev.get("dim") != str(self.cfg.dim))

    def _purge_file(self, con, base: str):
        """Remove all derived rows for one file (chunks, fts, vec)."""
        ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE path=?", (base,))]
        for cid in ids:
            con.execute("INSERT INTO fts_chunks(fts_chunks, rowid, text) "
                        "VALUES('delete', ?, (SELECT text FROM chunks WHERE id=?))", (cid, cid))
            con.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (cid,))
        con.execute("DELETE FROM chunks WHERE path=?", (base,))

    def _index_file(self, con, fp: str, ev_re) -> tuple[int, int, int]:
        base = os.path.basename(fp)
        evergreen = 1 if ev_re.match(base) else 0
        age = _file_age_days(fp)
        text = open(fp, encoding="utf-8", errors="ignore").read()
        n_chunks = n_new = n_cache = 0
        for ch in chunk_markdown(text, self.cfg.chunk_chars):
            h = _sha(ch)
            stable = _sha(f"{base}\0{h}")          # deterministic, reindex-stable id
            cur = con.execute(
                "INSERT OR IGNORE INTO chunks(cid,path,hash,text,age_days,evergreen) "
                "VALUES(?,?,?,?,?,?)", (stable, base, h, ch, age, evergreen))
            if cur.rowcount == 0:      # exact (path,hash) already present this pass
                continue
            rid = cur.lastrowid; n_chunks += 1
            con.execute("INSERT INTO fts_chunks(rowid,text) VALUES(?,?)", (rid, ch))
            cached = con.execute(
                "SELECT vec FROM emb_cache WHERE hash=? AND model=?", (h, self.cfg.model)).fetchone()
            if cached:
                blob = cached[0]; n_cache += 1
            else:
                blob = _pack(embed([ch], self.cfg.model)[0]); n_new += 1
                con.execute("INSERT OR REPLACE INTO emb_cache(hash,model,vec) VALUES(?,?,?)",
                            (h, self.cfg.model, blob))
            con.execute("INSERT INTO vec_chunks(chunk_id,embedding) VALUES(?,?)", (rid, blob))
        return n_chunks, n_new, n_cache

    # ---- incremental indexing (fleet-safe: only touches changed files) ----
    def reindex(self, full: bool = False) -> dict:
        con = self._connect()
        ev_re = re.compile(self.cfg.evergreen_re, re.IGNORECASE)
        # BEGIN IMMEDIATE grabs the write lock up front, so two processes
        # reindexing the same tenant serialize (the loser waits out busy_timeout)
        # instead of racing into a vec_chunks rowid collision.
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            # Model/dim mismatch wipe happens INSIDE this atomic txn (not in
            # _connect): if the process dies mid-rebuild, SQLite rolls back to the
            # previous model's index — no half-wiped tables, no malformed vec0.
            # Forces a full rebuild so every chunk is re-embedded under the new model.
            if self._model_mismatch(con):
                full = True
                # NB: use execute() not executescript() — executescript issues an
                # implicit COMMIT first, which would break the surrounding
                # BEGIN IMMEDIATE and defeat the whole atomicity fix.
                for tbl in ("chunks", "files", "emb_cache", "vec_chunks", "fts_chunks"):
                    con.execute(f"DELETE FROM {tbl}")
                for kv in {"model": self.cfg.model, "dim": str(self.cfg.dim),
                           "schema": str(SCHEMA_VERSION)}.items():
                    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", kv)
            disk = {os.path.basename(p): (p, os.path.getmtime(p))
                    for p in glob.glob(os.path.join(self.dir, "*.md"))}
            known = {r[0]: r[1] for r in con.execute("SELECT path, mtime FROM files")}
            if full:
                for b in list(known):
                    self._purge_file(con, b)
                con.execute("DELETE FROM files")
                known = {}
            n_chunks = n_new = n_cache = n_files = 0
            for base, (fp, mt) in disk.items():
                if known.get(base) == mt:
                    continue                          # unchanged — skip entirely
                self._purge_file(con, base)           # stale rows out (no-op if new)
                c, nw, cc = self._index_file(con, fp, ev_re)
                con.execute("INSERT OR REPLACE INTO files(path,mtime) VALUES(?,?)", (base, mt))
                n_chunks += c; n_new += nw; n_cache += cc; n_files += 1
            for base in list(known):                  # files gone from disk
                if base not in disk:
                    self._purge_file(con, base)
                    con.execute("DELETE FROM files WHERE path=?", (base,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return {"files_total": len(disk), "files_reindexed": n_files,
                "chunks": n_chunks, "embedded": n_new, "cached": n_cache}

    # ---- search ----
    def search(self, query: str, k: int = 5) -> list[dict]:
        _t0 = time.perf_counter()
        try:
            out = self._search(query, k)
        except sqlite3.DatabaseError:
            # Resilient read-path (fleet-safe): a degraded/corrupt index (e.g. an
            # interrupted reindex left vec0 shadow tables inconsistent → "database
            # disk image is malformed") must NOT propagate to the agent. Return
            # empty and trigger ONE background rebuild — never block the caller and
            # never reindex inline (heavy embed work under concurrent load = DoS).
            self._heal_async()
            out = []
        from .obs import log_recall
        log_recall(self.dir, query, out, (time.perf_counter() - _t0) * 1000)
        return out

    def _heal_async(self) -> None:
        """Fire-and-forget background rebuild of a degraded index. Best-effort:
        a single daemon thread does a full reindex; a concurrent reindexer just
        serializes on the write lock (busy_timeout). Errors are swallowed —
        healing must never raise into the read-path."""
        def _run():
            try:
                self.reindex(full=True)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _search(self, query: str, k: int = 5) -> list[dict]:
        con = self._connect()
        try:
            qv = _pack(embed([query], self.cfg.model)[0])
            vec_rows = con.execute(
                "SELECT chunk_id FROM vec_chunks WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (qv, self.cfg.vec_pool)).fetchall()
            terms = _fts_query(query)
            fts_rows = []
            if terms:
                try:
                    fts_rows = con.execute(
                        "SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                        (terms, self.cfg.fts_pool)).fetchall()
                except sqlite3.OperationalError:
                    fts_rows = []   # defensive: never let a bad query break recall
            # RRF fusion
            fused: dict[int, float] = {}
            for r, (cid,) in enumerate(vec_rows):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.cfg.rrf_k + r)
            for r, (cid,) in enumerate(fts_rows):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.cfg.rrf_k + r)
            if not fused:
                return []
            # temporal decay (evergreen exempt)
            if self.cfg.half_life_days > 0:
                lam = math.log(2) / self.cfg.half_life_days
                for cid in list(fused):
                    row = con.execute("SELECT age_days,evergreen FROM chunks WHERE id=?", (cid,)).fetchone()
                    if row and not row[1]:
                        fused[cid] *= math.exp(-lam * row[0])
            ranked = sorted(fused, key=lambda c: -fused[c])
            # MMR diversity over the fused candidates
            selected = self._mmr(con, ranked, k)
            out = []
            for rid in selected:
                row = con.execute("SELECT cid,path,text FROM chunks WHERE id=?", (rid,)).fetchone()
                if row:
                    text = row[2]
                    snip = text if self.cfg.snippet_chars <= 0 else text[: self.cfg.snippet_chars]
                    out.append({"id": row[0], "score": round(fused[rid], 5), "file": row[1],
                                "text": text, "snippet": snip})
            return out
        finally:
            con.close()

    def _mmr(self, con, ranked: list[int], k: int) -> list[int]:
        if self.cfg.mmr_lambda >= 1.0 or len(ranked) <= k:
            return ranked[:k]
        # Jaccard token overlap as cheap redundancy signal (no extra embeds)
        def toks(cid):
            row = con.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()
            return set(re.findall(r"\w+", (row[0] if row else "").lower()))
        cand = ranked[: max(k * 4, 12)]
        tok = {c: toks(c) for c in cand}
        selected: list[int] = []
        rel = {c: 1.0 - i / len(cand) for i, c in enumerate(cand)}  # rank-based relevance
        while cand and len(selected) < k:
            best, best_score = None, -1e9
            for c in cand:
                div = 0.0
                if selected:
                    div = max(
                        len(tok[c] & tok[s]) / max(1, len(tok[c] | tok[s])) for s in selected)
                s = self.cfg.mmr_lambda * rel[c] - (1 - self.cfg.mmr_lambda) * div
                if s > best_score:
                    best, best_score = c, s
            selected.append(best); cand.remove(best)
        return selected
