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
import os, re, sys, struct, hashlib, sqlite3, glob, math, time, threading
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


# Per-tenant config: a `.cogvault.toml` in the tenant dir lets the model (and other
# Config fields) travel WITH the data, instead of relying on every caller exporting
# COGVAULT_MODEL. Without it, a bare CLI call falls back to DEFAULT_MODEL and a
# model-mismatch triggers a full re-embed wipe — the exact footgun this file prevents.
TENANT_CONFIG_NAME = ".cogvault.toml"

# Only these Config fields may be set from a tenant config file. Keep this allowlist
# tight: it is data that ships next to the markdown, so it must not be able to point
# the index cache elsewhere (db_dir) or do anything surprising.
_TENANT_CONFIG_KEYS = {
    "model": str, "dim": int, "chunk_chars": int, "rrf_k": int,
    "vec_pool": int, "fts_pool": int, "half_life_days": float, "mmr_lambda": float,
    "snippet_chars": int, "evergreen_re": str, "recursive": bool,
    "strip_frontmatter": bool, "ignore_globs": tuple,
}


def _parse_toml_minimal(text: str) -> dict:
    """Fallback flat-TOML reader for Python 3.10 (no stdlib tomllib).

    Handles only what a tenant config needs: top-level `key = value` lines where
    value is a quoted string, int, float, bool, or a simple inline array of strings.
    Good enough for `model = "..."`; complex TOML should use a 3.11+ runtime.
    """
    def _strip_comment(raw: str) -> str:
        # drop a trailing # comment, but not a # inside a quoted value
        q = None
        for i, c in enumerate(raw):
            if q:
                if c == q:
                    q = None
            elif c in "\"'":
                q = c
            elif c == "#":
                return raw[:i]
        return raw

    out: dict = {}
    for raw in text.splitlines():
        line = _strip_comment(raw).strip()
        if not line or "=" not in line or line.startswith("["):
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not val:
            continue
        if val[0] in "\"'":
            out[key] = val.strip("\"'")
        elif val.startswith("["):
            inner = val.strip("[]").strip()
            out[key] = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
        elif val in ("true", "false"):
            out[key] = (val == "true")
        else:
            try:
                out[key] = int(val)
            except ValueError:
                try:
                    out[key] = float(val)
                except ValueError:
                    out[key] = val
    return out


def _load_tenant_config(tenant_dir: str) -> dict:
    """Read `<tenant>/.cogvault.toml` → a dict of allowlisted Config overrides.

    Returns {} if the file is absent or unreadable. Unknown keys are ignored;
    known keys are coerced to their declared type (bad values are skipped, not fatal).
    A present-but-unusable file is LOUD: silently ignoring it means the model pin
    silently falls back to DEFAULT_MODEL, whose mismatch wipe re-embeds the whole
    index — the exact footgun this file exists to prevent.
    """
    path = os.path.join(tenant_dir, TENANT_CONFIG_NAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            import tomllib            # stdlib, Python 3.11+
            data = tomllib.loads(raw.decode("utf-8"))
        except ModuleNotFoundError:
            data = _parse_toml_minimal(raw.decode("utf-8"))
    except Exception as e:
        print(f"cogvault: WARNING — cannot parse {path} ({e}); tenant config IGNORED. "
              f"Settings fall back to env/defaults, which can trigger a full re-embed.",
              file=sys.stderr)
        return {}
    out: dict = {}
    for key, typ in _TENANT_CONFIG_KEYS.items():
        if key not in data:
            continue
        v = data[key]
        try:
            out[key] = tuple(v) if typ is tuple else typ(v)
        except (TypeError, ValueError):
            print(f"cogvault: WARNING — {path}: bad value for '{key}' ignored.",
                  file=sys.stderr)
            continue                  # malformed value → ignore that key, keep going
    if not out:
        print(f"cogvault: WARNING — {path} exists but yields no usable settings; "
              f"check for a mangled file (e.g. lost newlines turning it into one comment).",
              file=sys.stderr)
    return out


def apply_tenant_config(cfg: "Config", tenant_dir: str, respect_env: bool = True,
                        skip: "set[str] | None" = None) -> "Config":
    """Mutate `cfg` in place with values from `<tenant>/.cogvault.toml`.

    `skip` names fields the caller has already set explicitly (those must not be
    overridden by the file). When `respect_env` is True, the `model` field also yields
    to $COGVAULT_MODEL so an operator's env still wins over the tenant file.
    Returns the same `cfg` for convenience.
    """
    file_cfg = _load_tenant_config(tenant_dir)
    if not file_cfg:
        return cfg
    skip = skip or set()
    env_model = respect_env and "COGVAULT_MODEL" in os.environ
    for key, val in file_cfg.items():
        if key in skip:
            continue
        if key == "model" and env_model:
            continue
        setattr(cfg, key, val)
    return cfg


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
    # ---- source options (opt-in; flat agent-memory tenants keep the defaults) ----
    recursive: bool = False          # True = walk subdirectories (e.g. an Obsidian vault)
    strip_frontmatter: bool = False  # True = drop a leading YAML --- … --- block before chunking
    ignore_globs: tuple[str, ...] = ()  # path globs to skip (e.g. ".obsidian/*", "Templates/*")


# ---- one shared embedder per (process, model) -------------------------------
# Keyed by model name: a single process may touch tenants pinned to different
# models (fleet reindex loops, tests). A single global here silently embedded
# tenant #2 with tenant #1's model AND persisted those vectors into emb_cache
# under the wrong model name — permanent silent recall degradation.
_EMBEDDERS: dict = {}
_EMBEDDERS_LOCK = threading.Lock()
def _embedder(model: str):
    emb = _EMBEDDERS.get(model)
    if emb is None:
        with _EMBEDDERS_LOCK:
            emb = _EMBEDDERS.get(model)
            if emb is None:
                from fastembed import TextEmbedding
                emb = TextEmbedding(model_name=model)
                _EMBEDDERS[model] = emb
    return emb

def embed(texts: list[str], model: str = DEFAULT_MODEL) -> list[list[float]]:
    return [list(v) for v in _embedder(model).embed(texts)]


# ---- helpers ----------------------------------------------------------------
def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def _pack(v) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)

def strip_frontmatter(text: str) -> str:
    """Drop a single leading YAML frontmatter block (--- … ---). Obsidian notes
    carry tags/created/etc. as frontmatter; embedding that YAML as prose pollutes
    recall. Only a block at the very start of the file is removed."""
    return _FRONTMATTER_RE.sub("", text, count=1)


def chunk_markdown(text: str, chunk_chars: int) -> list[str]:
    """Split on blank lines, then pack paragraphs up to chunk_chars.
    A single paragraph longer than chunk_chars is hard-split: the embedding
    model truncates at its token limit, so an oversized chunk's tail would be
    invisible to the vector channel."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > chunk_chars:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(p[:chunk_chars].strip())
            p = p[chunk_chars:].strip()
        if not p:
            continue
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


# one heal thread per index db, process-wide (see Vault._heal_async)
_HEALS: dict = {}
_HEALS_LOCK = threading.Lock()


# ---- the store --------------------------------------------------------------
class Vault:
    """One Vault == one tenant directory of Markdown files."""

    def __init__(self, tenant_dir: str, config: Config | None = None):
        # realpath: symlinked and resolved spellings of the same tenant must map
        # to the SAME index db, or the two copies flap against each other.
        self.dir = os.path.realpath(os.path.expanduser(tenant_dir))
        self.cfg = config or Config()
        if not os.path.isdir(self.dir):
            print(f"cogvault: creating new tenant dir {self.dir} "
                  f"(typo'd --tenant paths create empty junk tenants).", file=sys.stderr)
        os.makedirs(self.dir, exist_ok=True)
        # Per-tenant `.cogvault.toml` overrides, applied only when the caller did NOT
        # pass an explicit Config. Precedence in this (library) path:
        #   $COGVAULT_MODEL  >  .cogvault.toml  >  built-in defaults.
        # Callers that build their own Config (the CLI, the MCP server) are responsible
        # for merging the file themselves via apply_tenant_config() in the right order —
        # that keeps an explicitly-pinned model (e.g. MCP) authoritative.
        if config is None:
            apply_tenant_config(self.cfg, self.dir, respect_env=True)
        # Index DB lives outside the markdown dir (no accidental git commit).
        # Filename is derived from the tenant path so tenants never collide.
        db_dir = os.path.expanduser(self.cfg.db_dir) if self.cfg.db_dir else DEFAULT_DB_DIR
        os.makedirs(db_dir, exist_ok=True)
        # On case-insensitive filesystems (macOS/Windows default) two case-variant
        # spellings are the SAME directory — casefold before hashing so they share
        # one index instead of maintaining two diverging ones.
        key = self.dir.casefold() if sys.platform in ("darwin", "win32") else self.dir
        tag = hashlib.sha256(key.encode()).hexdigest()[:16]
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
        # Schema creation + user_version stamp happen ONLY when the db is new.
        # Doing either on every connect (a) takes a write lock on the pure READ
        # path, so searches stall behind a long reindex, and (b) overwrites the
        # old user_version before any future migration could read it.
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
                       ).fetchone():
            return con
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

    def _iter_files(self):
        """Yield (key, fullpath) for every .md to index. key is the file's path
        RELATIVE to the tenant dir when recursive (so notes with the same basename
        in different folders don't collide), else the basename (flat, legacy).
        ignore_globs are matched against the relative path."""
        import fnmatch
        if self.cfg.recursive:
            for root, dirs, names in os.walk(self.dir):
                # prune ignored / hidden dirs in-place so os.walk doesn't descend them
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for nm in names:
                    if not nm.endswith(".md"):
                        continue
                    fp = os.path.join(root, nm)
                    rel = os.path.relpath(fp, self.dir)
                    if any(fnmatch.fnmatch(rel, g) for g in self.cfg.ignore_globs):
                        continue
                    yield rel, fp
        else:
            for fp in glob.glob(os.path.join(self.dir, "*.md")):
                base = os.path.basename(fp)
                if any(fnmatch.fnmatch(base, g) for g in self.cfg.ignore_globs):
                    continue
                yield base, fp

    def _purge_file(self, con, base: str):
        """Remove all derived rows for one file (chunks, fts, vec)."""
        ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE path=?", (base,))]
        for cid in ids:
            con.execute("INSERT INTO fts_chunks(fts_chunks, rowid, text) "
                        "VALUES('delete', ?, (SELECT text FROM chunks WHERE id=?))", (cid, cid))
            con.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (cid,))
        con.execute("DELETE FROM chunks WHERE path=?", (base,))

    def _prepare_file(self, con, fp: str) -> list[tuple[str, str, bytes, bool]]:
        """Chunk a file and resolve each chunk's vector (emb_cache hit or fresh
        embed) → [(chunk, hash, blob, from_cache)]. Read-only: called OUTSIDE the
        write transaction, so embedding (the slow part) never holds the write lock
        and concurrent searches aren't starved during a big reindex."""
        text = open(fp, encoding="utf-8", errors="ignore").read()
        if self.cfg.strip_frontmatter:
            text = strip_frontmatter(text)
        out = []
        for ch in chunk_markdown(text, self.cfg.chunk_chars):
            h = _sha(ch)
            cached = con.execute(
                "SELECT vec FROM emb_cache WHERE hash=? AND model=?", (h, self.cfg.model)).fetchone()
            if cached:
                out.append((ch, h, cached[0], True))
            else:
                out.append((ch, h, _pack(embed([ch], self.cfg.model)[0]), False))
        return out

    def _index_file(self, con, key: str, fp: str, ev_re,
                    prepared: list | None = None) -> tuple[int, int, int]:
        evergreen = 1 if ev_re.match(os.path.basename(key)) else 0
        age = _file_age_days(fp)
        if prepared is None:           # race fallback: file entered scope inside the txn
            prepared = self._prepare_file(con, fp)
        n_chunks = n_new = n_cache = 0
        for ch, h, blob, from_cache in prepared:
            stable = _sha(f"{key}\0{h}")           # deterministic, reindex-stable id
            cur = con.execute(
                "INSERT OR IGNORE INTO chunks(cid,path,hash,text,age_days,evergreen) "
                "VALUES(?,?,?,?,?,?)", (stable, key, h, ch, age, evergreen))
            if cur.rowcount == 0:      # exact (path,hash) already present this pass
                continue
            rid = cur.lastrowid; n_chunks += 1
            con.execute("INSERT INTO fts_chunks(rowid,text) VALUES(?,?)", (rid, ch))
            if from_cache:
                n_cache += 1
            else:
                n_new += 1
                con.execute("INSERT OR REPLACE INTO emb_cache(hash,model,vec) VALUES(?,?,?)",
                            (h, self.cfg.model, blob))
            con.execute("INSERT INTO vec_chunks(chunk_id,embedding) VALUES(?,?)", (rid, blob))
        return n_chunks, n_new, n_cache

    # ---- incremental indexing (fleet-safe: only touches changed files) ----
    def reindex(self, full: bool = False) -> dict:
        con = self._connect()
        ev_re = re.compile(self.cfg.evergreen_re, re.IGNORECASE)
        con.isolation_level = None
        try:
            # ---- read phase (no write lock) ----------------------------------
            # Decide scope and compute every needed embedding BEFORE taking the
            # write lock: embedding is the slow part, and doing it inside the txn
            # starved concurrent searches for the whole rebuild.
            if self._model_mismatch(con):
                prev = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
                print(f"cogvault: model/dim mismatch on {self.dir} "
                      f"(index: {prev.get('model')}/{prev.get('dim')} → "
                      f"config: {self.cfg.model}/{self.cfg.dim}) — FULL re-embed. "
                      f"If unintended, check .cogvault.toml / $COGVAULT_MODEL.",
                      file=sys.stderr)
                full = True
            disk = {key: (fp, os.path.getmtime(fp)) for key, fp in self._iter_files()}
            known = {r[0]: r[1] for r in con.execute("SELECT path, mtime FROM files")}
            prepared = {key: self._prepare_file(con, fp)
                        for key, (fp, mt) in disk.items()
                        if full or known.get(key) != mt}
            # ---- write phase -------------------------------------------------
            # BEGIN IMMEDIATE grabs the write lock up front, so two processes
            # reindexing the same tenant serialize (the loser waits out
            # busy_timeout) instead of racing into a vec_chunks rowid collision.
            # The mismatch wipe stays INSIDE this atomic txn: if the process dies
            # mid-rebuild, SQLite rolls back to the previous model's index — no
            # half-wiped tables, no malformed vec0.
            con.execute("BEGIN IMMEDIATE")
            if self._model_mismatch(con):   # re-check under the lock (lost race)
                full = True
            if full:
                # Truly-full wipe of ALL derived rows — not just paths listed in
                # `files` — so orphans from older versions can't survive the
                # rebuild. Order matters: fts_chunks is an external-content FTS5
                # table; its 'delete-all' must run BEFORE chunks is cleared
                # (deleting chunks first leaves stale term postings that
                # misattribute to reused rowids). emb_cache is spared: it is
                # keyed (hash, model), so other models' entries remain valid and
                # switching back is cheap.
                # NB: execute() not executescript() — executescript issues an
                # implicit COMMIT first, which would break BEGIN IMMEDIATE.
                con.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('delete-all')")
                for tbl in ("chunks", "vec_chunks", "files"):
                    con.execute(f"DELETE FROM {tbl}")
                for kv in {"model": self.cfg.model, "dim": str(self.cfg.dim),
                           "schema": str(SCHEMA_VERSION)}.items():
                    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", kv)
                known = {}
            else:
                # re-read under the lock: another process may have indexed files
                # between the phases; trust the locked view
                known = {r[0]: r[1] for r in con.execute("SELECT path, mtime FROM files")}
            n_chunks = n_new = n_cache = n_files = 0
            for key, (fp, mt) in disk.items():
                if known.get(key) == mt:
                    continue                          # unchanged — skip entirely
                self._purge_file(con, key)            # stale rows out (no-op if new)
                c, nw, cc = self._index_file(con, key, fp, ev_re, prepared.get(key))
                con.execute("INSERT OR REPLACE INTO files(path,mtime) VALUES(?,?)", (key, mt))
                n_chunks += c; n_new += nw; n_cache += cc; n_files += 1
            for key in list(known):                   # files gone from disk
                if key not in disk:
                    self._purge_file(con, key)
                    con.execute("DELETE FROM files WHERE path=?", (key,))
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
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
        """Fire-and-forget background rebuild of a degraded index. Single-flight
        per db (process-wide): without the guard, every failed search under load
        spawned another full re-embed — a thundering herd serialized on the write
        lock. Errors are swallowed — healing must never raise into the read-path.
        NB: the thread is a daemon; a short-lived CLI process exits before it
        finishes — call join_heal() to wait for the rebuild."""
        def _run():
            try:
                self.reindex(full=True)
            except Exception:
                pass
        with _HEALS_LOCK:
            t = _HEALS.get(self.db_path)
            if t is None or not t.is_alive():
                t = threading.Thread(target=_run, daemon=True)
                _HEALS[self.db_path] = t
                t.start()
            self._heal_thread = t

    def join_heal(self, timeout: float | None = None) -> bool:
        """Wait for a heal triggered by a failed search on THIS vault. Returns
        True if a heal ran and finished (worth retrying the search)."""
        t = getattr(self, "_heal_thread", None)
        if t is None:
            return False
        t.join(timeout)
        return not t.is_alive()

    def _search(self, query: str, k: int = 5) -> list[dict]:
        con = self._connect()
        try:
            # Same-dim model drift gives silent garbage vector ranking (no error
            # is possible — the dims match). Warn once per Vault so config drift
            # is visible instead of quietly degrading recall.
            if not getattr(self, "_warned_mismatch", False) and self._model_mismatch(con):
                self._warned_mismatch = True
                print(f"cogvault: WARNING — searching {self.dir} with model "
                      f"{self.cfg.model!r} but the index was built with a different "
                      f"model; vector ranking is unreliable. Run `cogvault index` "
                      f"to rebuild (or fix .cogvault.toml/$COGVAULT_MODEL).",
                      file=sys.stderr)
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
