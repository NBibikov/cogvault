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
from json import dumps as json_dumps
from dataclasses import dataclass, field

import sqlite_vec

# Multilingual by default: agent memory is rarely English-only. This model embeds
# Cyrillic, CJK, etc. correctly. Same 384-d as bge-small-en (no vec-table change),
# no query/passage prefixes required. Override via Config.model for English-only fleets.
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384-d
DIM = 384
SCHEMA_VERSION = 3
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
    "snippet_chars": int, "fts_stem": bool, "summary_chunk": bool, "query_prefix": str, "doc_prefix": str, "evergreen_re": str, "recursive": bool,
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
    # Asymmetric models (the e5 family) are trained with "query: "/"passage: "
    # prefixes and lose several points of recall without them. None = use the
    # model's known prefixes (see _MODEL_PREFIXES), "" = force none.
    # Experimental: prefix-stem Cyrillic terms in the BM25 channel (see
    # _stem_term). Off until a judged eval shows it helps.
    fts_stem: bool = False
    # Index "name — description" from the frontmatter as its own chunk. Real
    # agent queries read like card titles ("деплой на прод"); a judged set of
    # 65 real queries (2026-10-05) put this at hit@5 0.923 vs 0.877 without it
    # (better on 7 queries, worse on 3). Search returns one hit per card, so
    # the extra chunk never duplicates a result.
    summary_chunk: bool = True
    query_prefix: str | None = None
    doc_prefix: str | None = None
    db_dir: str = ""                 # "" = ~/.cache/cogvault; set to a dir to override
    # feedback_* is evergreen by nature: standing rules ("never build unasked",
    # "no Android commits") don't expire because nobody touched the file in 90
    # days — decaying them buries exactly the instructions memory exists to keep.
    evergreen_re: str = r"^(MEMORY|INDEX|.*reference_|.*feedback_|.*architecture).*"
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
def _pkg_version() -> str:
    from . import __version__
    return __version__


def embed_backend() -> str:
    """Identity of the embedding BACKEND, stamped into each index's meta.

    `model` + `dim` cannot detect a library that changes what a model outputs.
    fastembed 0.6 switched MiniLM from CLS-token to mean pooling and stopped
    L2-normalizing the multilingual model: same model name, same 384 dims, but
    vectors that no longer live in the same space as the stored ones. A fleet
    audit on 2026-09-01 found 35-50% of vectors on every multilingual tenant
    diverging from freshly embedded text (cos as low as 0.198) with stored norms
    ranging 2.07-3.06 within a single database — vec0 ranks by L2, so a query of
    norm 5.3 against documents of norm 1.0 ranks mostly by magnitude, not
    meaning. Stamping the library version makes that a detected mismatch (an
    automatic rebuild) instead of silent, unexplained recall rot."""
    try:
        from importlib.metadata import version
        return f"fastembed/{version('fastembed')}"
    except Exception:
        return "fastembed/unknown"


def model_cache_dir() -> str:
    """Where fastembed unpacks its ONNX weights.

    Without an explicit cache_dir fastembed falls back to a temp directory. On
    macOS that is $TMPDIR (/var/folders/...), which the OS periodically purges —
    so an index or recall that ran fine yesterday dies today with
    `NoSuchFile: [ONNXRuntimeError] Load model from /var/folders/...`. Four such
    failures (two tenants) sat in the query log before this was
    pinned. Keep the weights next to the indexes, under a stable cache root."""
    # Deliberately NOT under $XDG_CACHE_HOME: that variable is redirected per
    # test-run (and per sandbox) to isolate INDEXES, but model weights are a
    # ~300 MB read-only download that must survive such redirection. Tying them
    # together made every isolated run re-download the weights — three
    # concurrent test processes then spent 25s+ each fetching the same files and
    # blew the concurrency test's timeout. Indexes are cheap to rebuild and
    # belong to a tenant; weights belong to the machine.
    return (os.environ.get("FASTEMBED_CACHE_PATH")
            or os.path.join(os.path.expanduser("~/.cache"), "fastembed"))


# Models fastembed does not ship but that are worth having. multilingual-e5-small
# is the same 384 dims as MiniLM (no schema change) but reads 512 tokens instead
# of 128 and, on a description→card eval over three live tenants (2026-10-05),
# lifted hit@1 from 0.75/0.66/0.76 to 0.79/0.75/0.85. Registered lazily, only
# when a tenant actually asks for it.
_CUSTOM_MODELS = {
    "intfloat/multilingual-e5-small": dict(
        hf="Xenova/multilingual-e5-small", dim=384, model_file="onnx/model.onnx"),
}
_MODEL_PREFIXES = {
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


def model_prefixes(cfg: "Config") -> tuple[str, str]:
    """(query_prefix, doc_prefix) for this config: explicit fields win, else the
    model's known prefixes, else none."""
    known = _MODEL_PREFIXES.get(cfg.model, ("", ""))
    q = known[0] if cfg.query_prefix is None else cfg.query_prefix
    d = known[1] if cfg.doc_prefix is None else cfg.doc_prefix
    return q, d


def _register_custom(model: str) -> None:
    spec = _CUSTOM_MODELS.get(model)
    if not spec:
        return
    from fastembed import TextEmbedding
    if any(m["model"] == model for m in TextEmbedding.list_supported_models()):
        return
    from fastembed.common.model_description import PoolingType, ModelSource
    try:
        TextEmbedding.add_custom_model(
            model=model, pooling=PoolingType.MEAN, normalization=True,
            sources=ModelSource(hf=spec["hf"]), dim=spec["dim"],
            model_file=spec["model_file"])
    except ValueError:
        pass                                   # already registered (race / re-import)


_TOKENIZERS: dict = {}


def max_tokens(model: str) -> int:
    """The model's input window. Text past it is silently truncated — the vector
    channel never sees it."""
    trunc = getattr(_embedder(model).model.tokenizer, "truncation", None) or {}
    return int(trunc.get("max_length") or 512)


def count_tokens(text: str, model: str) -> int:
    """Token count WITHOUT truncation. Uses a private copy of the model's
    tokenizer: disabling truncation on the shared one would change what the
    embedder itself sees."""
    tok = _TOKENIZERS.get(model)
    if tok is None:
        from tokenizers import Tokenizer
        tok = Tokenizer.from_str(_embedder(model).model.tokenizer.to_str())
        tok.no_truncation()
        tok.no_padding()
        _TOKENIZERS[model] = tok
    return len(tok.encode(text).ids)


def fit_to_tokens(chunks: list[str], model: str, budget: int) -> list[str]:
    """Split every chunk that exceeds `budget` tokens into pieces that fit.

    paraphrase-multilingual-MiniLM truncates at 128 tokens while chunks were
    packed to 1500 characters (~400-500 tokens of Ukrainian). An audit on
    2026-10-05 found 91-94% of chunks over the limit on every multilingual
    tenant: only ~35% of stored text was visible to the vector channel, the
    rest reachable by exact-keyword BM25 alone. Splits on lines, then
    sentences, then words, so pieces stay readable."""
    out: list[str] = []
    for ch in chunks:
        if count_tokens(ch, model) <= budget:
            out.append(ch)
            continue
        out.extend(_split_to_budget(ch, model, budget))
    return out


def _split_to_budget(text: str, model: str, budget: int) -> list[str]:
    if count_tokens(text, model) <= budget:
        return [text.strip()] if text.strip() else []
    for sep_re in (r"\n", r"(?<=[.!?…])\s+", r"\s+"):
        atoms = [a for a in re.split(sep_re, text) if a.strip()]
        if len(atoms) > 1:
            break
    else:
        # one unbreakable token run: hard-split by characters
        half = max(1, len(text) // 2)
        return (_split_to_budget(text[:half], model, budget)
                + _split_to_budget(text[half:], model, budget))
    joiner = "\n" if sep_re == r"\n" else " "
    pieces, cur = [], ""
    for a in atoms:
        cand = (cur + joiner + a) if cur else a
        if count_tokens(cand, model) <= budget:
            cur = cand
            continue
        if cur:
            pieces.append(cur.strip())
        if count_tokens(a, model) <= budget:
            cur = a
        else:
            pieces.extend(_split_to_budget(a, model, budget))
            cur = ""
    if cur.strip():
        pieces.append(cur.strip())
    return pieces


def chunker_id(model: str, summary: bool = True) -> str:
    """Stamped into meta: a chunker change re-chunks (and so re-embeds) the
    tenant, just like a model change does."""
    return f"tok-v1/{max_tokens(model)}" + ("+sum" if summary else "")


def card_summary(text: str) -> str | None:
    """`name — description` from leading frontmatter, or None."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    parts = []
    for key in ("name", "description"):
        f = re.search(rf"^{key}:[ \t]*(.+)$", m.group(0), re.M)
        v = f.group(1).strip().strip("\"'").strip() if f else ""
        if v:
            parts.append(v)
    return " — ".join(parts) or None


def _embedder(model: str):
    emb = _EMBEDDERS.get(model)
    if emb is None:
        with _EMBEDDERS_LOCK:
            emb = _EMBEDDERS.get(model)
            if emb is None:
                import warnings
                # fastembed >=0.6 warns that MiniLM now mean-pools instead of
                # using the CLS token. Verified 2026-09-01 against three live
                # tenants (both pinned models):
                # cos(stored_vector, fresh embed of the same text) == 1.0000 on
                # every sampled chunk, so our indexes already match the current
                # behaviour and need no re-embed. The warning fires on every CLI
                # call and buries real output in subagent scrollback — drop it.
                # If fastembed ever changes pooling FOR REAL, the guard in
                # _model_changed() (model+dim in meta) will not catch it: re-run
                # that cosine check after a major fastembed bump.
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", message=".*mean pooling.*", category=UserWarning)
                    from fastembed import TextEmbedding
                    _register_custom(model)
                    cache = model_cache_dir()
                    os.makedirs(cache, exist_ok=True)
                    emb = TextEmbedding(model_name=model, cache_dir=cache)
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


def parse_frontmatter_type(text: str) -> str | None:
    """Extract the card type from a leading YAML frontmatter block, without a
    YAML dependency (same minimal-parser philosophy as _parse_toml_minimal).
    Recognizes both shapes used by memory cards:
        type: reference                    (flat)
        metadata:\n  type: reference       (nested)
    Flat wins when both exist (deterministic). Returns a lowercased token, or
    None for no frontmatter / no type / empty / null values. Any token is
    accepted — no allowlist — so legacy cards degrade to None naturally."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    block = m.group(0).split("---", 2)[1]

    def _clean(v: str) -> str | None:
        v = v.strip().strip("\"'").strip().lower()
        return v if v and v not in ("null", "~") else None

    flat = None
    nested = None
    in_meta = False
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        if not indented:
            in_meta = False
            key, sep, val = line.partition(":")
            if not sep:
                continue
            key = key.strip()
            if key == "type" and flat is None:
                flat = _clean(val)
            elif key == "metadata" and not val.strip():
                in_meta = True
        elif in_meta:
            key, sep, val = line.strip().partition(":")
            if sep and key.strip() == "type" and nested is None:
                nested = _clean(val)
    return flat or nested


# [[slug]], [[slug|alias]], [[slug#section]] — capture the slug only.
def parse_frontmatter_name(text: str) -> str | None:
    """Extract the card's declared `name:` slug from leading YAML frontmatter.

    Memory cards are linked by this slug ([[card-name]]), which by convention
    differs from the filename (`project_foo_bar.md` declares
    `name: project-foo-bar`). Resolving links by filename alone dropped 2 of
    every 3 links on a real tenant, so the graph the recall "Related:" line is
    built from was mostly invisible. Same minimal-parser philosophy as
    parse_frontmatter_type: no YAML dependency, tolerant of quotes."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    block = m.group(0).split("---", 2)[1]
    for line in block.splitlines():
        if line[:1] in (" ", "\t") or not line.strip():
            continue
        key, sep, val = line.partition(":")
        if sep and key.strip() == "name":
            v = val.strip().strip("\"'").strip()
            return v or None
    return None


_WIKILINK_RE = re.compile(r"\[\[([^\]\[|#\n]+)")
_CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)

_LINK_TYPES = ("project", "feedback", "reference", "user")


def link_variants(target: str) -> list[str]:
    """Spellings a [[link]] may legitimately mean, most literal first: as
    written, with -/_ swapped, then with a card-type prefix. Agents routinely
    drop the prefix ([[build-means-fastlane-beta]] for
    feedback_build_means_fastlane_beta.md): ~30 of the fleet's ghost links on
    2026-10-05 were exactly that."""
    base = [target, target.replace("-", "_"), target.replace("_", "-")]
    out = list(dict.fromkeys(base))
    if not target.lower().startswith(_LINK_TYPES):
        for t in _LINK_TYPES:
            for b in (f"{t}-{target}", f"{t}_{target}"):
                for v in (b, b.replace("-", "_"), b.replace("_", "-")):
                    if v not in out:
                        out.append(v)
    return out


def parse_wikilinks(text: str) -> list[str]:
    """All [[wiki-link]] target slugs in a document, deduped, order preserved.
    Slugs are returned as written; resolution to real files happens at search
    time against the index (targets are filename stems by convention)."""
    # Links quoted as code are examples, not links: cards documenting the link
    # syntax itself (`[[slug]]`) showed up as ghost links to "slug".
    text = _CODE_RE.sub("", text)
    seen: dict[str, None] = {}
    for slug in _WIKILINK_RE.findall(text):
        slug = slug.strip()
        if slug:
            seen.setdefault(slug)
    return list(seen)


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
            # cut at the last whitespace, not mid-word ("Р ечення" broke both
            # the token and the keyword channel for the split word)
            cut = p.rfind(" ", chunk_chars // 2, chunk_chars)
            cut = cut if cut > 0 else chunk_chars
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
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

def _stem_term(t: str) -> str:
    """Crude prefix stem for inflected (Cyrillic) words: FTS5's unicode61
    tokenizer has no morphology, so "тестів" never matched "тест"/"тести".
    Long Cyrillic words lose up to two trailing letters and become a prefix
    query; Latin terms are left exact."""
    if re.search(r"[а-яіїєґ]", t) and len(t) >= 6:
        return f'"{t[:-2]}"*'
    if re.search(r"[а-яіїєґ]", t) and len(t) == 5:
        return f'"{t[:-1]}"*'
    return f'"{t}"'


def _fts_query(query: str, stem: bool = False) -> str:
    """Build a safe FTS5 MATCH expression: each meaningful term double-quoted
    (so FTS5 keywords like OR/NEAR/AND and punctuation can't break parsing),
    stopwords dropped. Returns '' when nothing meaningful remains."""
    terms = [t for t in re.findall(r"\w+", query.lower())
             if t not in _STOPWORDS and len(t) > 1]
    if not terms:                                  # all-stopword query: keep originals
        terms = re.findall(r"\w+", query.lower())
    # double-quote each term; FTS5 treats a quoted token as a literal phrase
    if stem:
        return " OR ".join(_stem_term(t) for t in terms)
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
            if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                self._migrate(con)
            return con
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")  # for future migrations
        con.executescript(f"""
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY,            -- internal rowid (vec0/fts5 need INT)
                cid TEXT,                          -- STABLE id = sha(path|hash), safe to reference
                path TEXT, hash TEXT,
                text TEXT, age_days REAL DEFAULT 0, evergreen INTEGER DEFAULT 0,
                type TEXT,                         -- frontmatter card type (nullable)
                UNIQUE(path, hash));
            CREATE INDEX IF NOT EXISTS idx_chunks_cid ON chunks(cid);
            CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(type);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL,
                name TEXT);   -- frontmatter `name:` slug; [[links]] target it
            CREATE TABLE IF NOT EXISTS emb_cache(
                hash TEXT, model TEXT, vec BLOB, PRIMARY KEY(hash, model));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS links(
                src_path TEXT NOT NULL,            -- indexed file key
                target   TEXT NOT NULL,            -- [[slug]] as written, no .md
                UNIQUE(src_path, target));
            CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_path);
            CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
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
                    "backend": embed_backend(), "chunker": chunker_id(self.cfg.model, self.cfg.summary_chunk),
                    "schema": str(SCHEMA_VERSION)}
            con.executemany("INSERT INTO meta(key,value) VALUES(?,?)", want.items())
        con.commit()
        return con

    def _migrate(self, con) -> None:
        """In-place schema upgrade v1 → v2 (type column + links table). Cheap by
        design: chunk text is unchanged, so every embedding stays valid in
        emb_cache — no re-embed. Invalidating every mtime in `files` forces the
        next reindex() to re-run every file through _index_file (cache hits
        only), which backfills the new `type` column and `links` rows. The keys
        MUST survive (UPDATE, not DELETE): reindex detects deletions by "key in
        `files` but not on disk", so clearing the table would orphan the chunks
        of any file deleted between the last v1 index and the backfill — they'd
        keep matching searches forever. Until the backfill runs, types are NULL
        (a `type` filter matches nothing) — the MCP server reindexes on boot,
        and CLI users run `cogvault index` after upgrading anyway."""
        con.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: two processes can race into _connect
            # on a v1 db; the loser must see the winner's work and no-op.
            if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
                con.execute("COMMIT")
                return
            cols = {r[1] for r in con.execute("PRAGMA table_info(chunks)")}
            if "type" not in cols:
                con.execute("ALTER TABLE chunks ADD COLUMN type TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(type)")
            con.execute("""CREATE TABLE IF NOT EXISTS links(
                src_path TEXT NOT NULL, target TEXT NOT NULL,
                UNIQUE(src_path, target))""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_links_src ON links(src_path)")
            fcols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
            if "name" not in fcols:
                # v3: cards are linked by their frontmatter `name:` slug, which by
                # convention differs from the filename. Without it _related()
                # resolved only 1 link in 3 on a real tenant.
                con.execute("ALTER TABLE files ADD COLUMN name TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
            con.execute("UPDATE files SET mtime = -1")  # force cheap backfill, keep keys
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema',?)",
                        (str(SCHEMA_VERSION),))
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

    def _model_mismatch(self, con) -> bool:
        """True if the index was built with a different model/dim than the current
        config — its cached vectors are incompatible and must be rebuilt."""
        prev = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
        if not prev:
            return False
        # An index written before backends were stamped has no "backend" key.
        # Treat that as a mismatch ONLY if the recorded backend differs from the
        # running one; a missing key means "unknown, pre-0.9.0" and is handled by
        # the one-off fleet rebuild rather than by silently re-embedding every
        # legacy tenant on first connect.
        prev_backend = prev.get("backend")
        return (prev.get("model") != self.cfg.model
                or prev.get("dim") != str(self.cfg.dim)
                or (prev_backend is not None and prev_backend != embed_backend()))

    def _chunker_stale(self, con) -> bool:
        """True when the index was chunked by an older chunker. A missing key
        (pre-0.10 index) IS stale: those chunks were packed past the model's
        token window, which is exactly what the rebuild fixes."""
        row = con.execute("SELECT value FROM meta WHERE key='chunker'").fetchone()
        return (row[0] if row else None) != chunker_id(self.cfg.model, self.cfg.summary_chunk)

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
        con.execute("DELETE FROM links WHERE src_path=?", (base,))

    def _prepare_file(self, con, fp: str) -> tuple[str | None, str | None, list[str], list[tuple]]:
        """Chunk a file and resolve each chunk's vector (emb_cache hit or fresh
        embed) → (card_type, card_name, wikilinks, [(chunk, hash, blob, from_cache)]).
        Type and links are parsed from the RAW text (before the optional
        frontmatter strip — the type lives IN the frontmatter). Read-only: called
        OUTSIDE the write transaction, so embedding (the slow part) never holds
        the write lock and concurrent searches aren't starved during a big reindex."""
        text = open(fp, encoding="utf-8", errors="ignore").read()
        ftype = parse_frontmatter_type(text)
        fname = parse_frontmatter_name(text)
        links = parse_wikilinks(text)
        summary = card_summary(text) if self.cfg.summary_chunk else None
        if self.cfg.strip_frontmatter:
            text = strip_frontmatter(text)
        chunks = fit_to_tokens(
            ([summary] if summary else []) + chunk_markdown(text, self.cfg.chunk_chars),
            self.cfg.model, self._token_budget())
        out: list = []
        missing: list[int] = []
        for ch in chunks:
            h = _sha(ch)
            cached = con.execute(
                "SELECT vec FROM emb_cache WHERE hash=? AND model=?", (h, self.cfg.model)).fetchone()
            if cached:
                out.append((ch, h, cached[0], True))
            else:
                missing.append(len(out))
                out.append((ch, h, None, False))
        if missing:
            # One batched call per file instead of one ONNX run per chunk.
            _, dp = model_prefixes(self.cfg)
            vecs = embed([dp + out[i][0] for i in missing], self.cfg.model)
            for i, v in zip(missing, vecs):
                ch, h, _, _ = out[i]
                out[i] = (ch, h, _pack(v), False)
        return ftype, fname, links, out

    def _token_budget(self) -> int:
        """Tokens a chunk may use: the model window minus the doc prefix and
        special tokens, so nothing gets truncated at embed time."""
        _, dp = model_prefixes(self.cfg)
        return max(32, max_tokens(self.cfg.model) - count_tokens(dp, self.cfg.model) - 2)

    def _index_file(self, con, key: str, fp: str, ev_re,
                    prepared: list | None = None) -> tuple[int, int, int, str | None]:
        evergreen = 1 if ev_re.match(os.path.basename(key)) else 0
        age = _file_age_days(fp)
        if prepared is None:           # race fallback: file entered scope inside the txn
            prepared = self._prepare_file(con, fp)
        ftype, fname, links, rows = prepared
        n_chunks = n_new = n_cache = 0
        for ch, h, blob, from_cache in rows:
            stable = _sha(f"{key}\0{h}")           # deterministic, reindex-stable id
            cur = con.execute(
                "INSERT OR IGNORE INTO chunks(cid,path,hash,text,age_days,evergreen,type) "
                "VALUES(?,?,?,?,?,?,?)", (stable, key, h, ch, age, evergreen, ftype))
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
        con.execute("DELETE FROM links WHERE src_path=?", (key,))
        if links:
            con.executemany("INSERT OR IGNORE INTO links(src_path,target) VALUES(?,?)",
                            [(key, t) for t in links])
        return n_chunks, n_new, n_cache, fname

    def _written_by_newer(self, con) -> str | None:
        """Version string of a NEWER cogvault that last rebuilt this index, else
        None. Long-lived MCP servers keep the code they started with; after an
        upgrade they disagree with the index on model/chunker and, unguarded,
        rebuild it back to the old scheme — then the new code rebuilds it
        forward again (seen during the 0.10 rollout). Older code must defer."""
        row = con.execute("SELECT value FROM meta WHERE key='writer'").fetchone()
        if not row:
            return None
        from . import __version__
        def _v(x):
            return tuple(int(p) for p in re.findall(r"\d+", x)[:3])
        return row[0] if _v(row[0]) > _v(__version__) else None

    # ---- incremental indexing (fleet-safe: only touches changed files) ----
    def reindex(self, full: bool = False) -> dict:
        con = self._connect()
        newer = self._written_by_newer(con)
        if newer and (self._model_mismatch(con) or self._chunker_stale(con)):
            con.close()
            print(f"cogvault: {self.dir} was built by cogvault {newer}; this "
                  f"process is older and will not rebuild it. Restart it to pick "
                  f"up the new code.", file=sys.stderr)
            return {"files_total": 0, "files_reindexed": 0, "chunks": 0,
                    "embedded": 0, "cached": 0, "skipped": f"index built by {newer}"}
        ev_re = re.compile(self.cfg.evergreen_re, re.IGNORECASE)
        con.isolation_level = None
        try:
            # ---- read phase (no write lock) ----------------------------------
            # Decide scope and compute every needed embedding BEFORE taking the
            # write lock: embedding is the slow part, and doing it inside the txn
            # starved concurrent searches for the whole rebuild.
            if self._model_mismatch(con):
                prev = {k: v for k, v in con.execute("SELECT key,value FROM meta")}
                # Name the backend when it is the reason: "MiniLM/384 → MiniLM/384"
                # read as a bug when only the fastembed/onnxruntime build differed.
                was = f"{prev.get('model')}/{prev.get('dim')}"
                now = f"{self.cfg.model}/{self.cfg.dim}"
                if was == now:
                    was, now = f"backend {prev.get('backend')}", f"backend {embed_backend()}"
                print(f"cogvault: embedding mismatch on {self.dir} "
                      f"(index: {was} → now: {now}) — FULL re-embed. "
                      f"If unintended, check .cogvault.toml / $COGVAULT_MODEL.",
                      file=sys.stderr)
                full = True
            elif self._chunker_stale(con):
                print(f"cogvault: chunker changed on {self.dir} — re-chunking "
                      f"(chunks are now fitted to the model's token window).",
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
            if self._model_mismatch(con) or self._chunker_stale(con):
                full = True                 # re-check under the lock (lost race)
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
                for tbl in ("chunks", "vec_chunks", "files", "links"):
                    con.execute(f"DELETE FROM {tbl}")
                for kv in {"model": self.cfg.model, "dim": str(self.cfg.dim),
                           "backend": embed_backend(),
                           "chunker": chunker_id(self.cfg.model, self.cfg.summary_chunk),
                           "writer": _pkg_version(),
                           "schema": str(SCHEMA_VERSION)}.items():
                    con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", kv)
                known = {}
            else:
                # re-read under the lock: another process may have indexed files
                # between the phases; trust the locked view
                known = {r[0]: r[1] for r in con.execute("SELECT path, mtime FROM files")}
            n_chunks = n_new = n_cache = n_files = 0
            # Cards touched this run, for write-side telemetry. Collected here but
            # logged only AFTER the COMMIT below: a rollback must not leave the
            # query log claiming writes that never landed. Suppressed entirely on
            # a full rebuild, where every file is "reindexed" and logging would
            # report a 572-card tenant as 572 fresh memories.
            touched: list[tuple[str, str, int]] = []   # (key, op, chunks)
            for key, (fp, mt) in disk.items():
                if known.get(key) == mt:
                    continue                          # unchanged — skip entirely
                is_new = key not in known
                self._purge_file(con, key)            # stale rows out (no-op if new)
                c, nw, cc, fname = self._index_file(con, key, fp, ev_re, prepared.get(key))
                con.execute("INSERT OR REPLACE INTO files(path,mtime,name) VALUES(?,?,?)",
                            (key, mt, fname))
                n_chunks += c; n_new += nw; n_cache += cc; n_files += 1
                # mtime -1 = a schema migration invalidated it (content did not
                # change). The v3 backfill on 2026-09-17 logged 1572 such
                # "updates" — 88% of all card edits the log has ever recorded.
                if not full and known.get(key) != -1:
                    touched.append((key, "create" if is_new else "update", c))
            for key in list(known):                   # files gone from disk
                if key not in disk:
                    self._purge_file(con, key)
                    con.execute("DELETE FROM files WHERE path=?", (key,))
                    if not full:
                        touched.append((key, "delete", 0))
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        # Write-side telemetry for the file-first path. Most cards are NOT written
        # through the MCP `cogvault_record` tool — agents (and the /remember skill)
        # write markdown directly and then run `cogvault index`, so the log showed
        # 616 recalls against 24 records and made active tenants look read-only.
        # Logging from reindex captures those writes wherever they came from.
        if touched:
            from .obs import log_record
            for key, op, nch in touched:
                log_record(self.dir, key, "", op=op, chunks=nch)
        return {"files_total": len(disk), "files_reindexed": n_files,
                "chunks": n_chunks, "embedded": n_new, "cached": n_cache}

    # ---- search ----
    def search(self, query: str, k: int = 5, card_type: str | None = None) -> list[dict]:
        card_type = (card_type or "").strip().lower() or None
        # Cold = this process has not loaded the model yet. Most recalls come
        # from one-shot CLI processes, so the logged p50 (~530 ms) was model
        # load, not search (~15 ms warm). Flag it so `analyze` can split them.
        cold = self.cfg.model not in _EMBEDDERS
        _t0 = time.perf_counter()
        try:
            out = self._search(query, k, card_type)
        except sqlite3.DatabaseError:
            # Resilient read-path (fleet-safe): a degraded/corrupt index (e.g. an
            # interrupted reindex left vec0 shadow tables inconsistent → "database
            # disk image is malformed") must NOT propagate to the agent. Return
            # empty and trigger ONE background rebuild — never block the caller and
            # never reindex inline (heavy embed work under concurrent load = DoS).
            self._heal_async()
            out = []
        from .obs import log_recall
        log_recall(self.dir, query, out, (time.perf_counter() - _t0) * 1000,
                   card_type=card_type, cold=cold)
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

    # When a type filter is active, pull deeper candidate pools before filtering:
    # the wanted type's chunks may sit below the unfiltered pool cutoff. 4× is a
    # heuristic — a very rare type buried deeper can still be missed (documented
    # limitation; vec0 MATCH can't take an arbitrary joined WHERE).
    _FILTER_OVERFETCH = 4

    def _search(self, query: str, k: int = 5, card_type: str | None = None) -> list[dict]:
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
            over = self._FILTER_OVERFETCH if card_type else 1
            qp, _ = model_prefixes(self.cfg)
            qv = _pack(embed([qp + query], self.cfg.model)[0])
            # `distance` comes back alongside the id: RRF scores are pure rank
            # reciprocals (capped at ~2/rrf_k) and say nothing about whether a
            # hit is actually relevant, so we keep the raw vector distance to
            # derive a real similarity for each result (see `sim` below).
            vec_hits = con.execute(
                "SELECT chunk_id, distance FROM vec_chunks WHERE embedding MATCH ? "
                "ORDER BY distance LIMIT ?",
                (qv, self.cfg.vec_pool * over)).fetchall()
            vec_dist = {cid: d for cid, d in vec_hits}
            vec_rows = [(cid,) for cid, _ in vec_hits]
            terms = _fts_query(query, stem=self.cfg.fts_stem)
            fts_rows = []
            if terms:
                try:
                    fts_rows = con.execute(
                        "SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                        (terms, self.cfg.fts_pool * over)).fetchall()
                except sqlite3.OperationalError:
                    fts_rows = []   # defensive: never let a bad query break recall
            if card_type:
                # Post-filter the candidate pools (rank order preserved), then
                # truncate back to the configured pool sizes so RRF/decay/MMR see
                # exactly what an unfiltered search of a type-pure tenant would.
                cand = {cid for (cid,) in vec_rows} | {cid for (cid,) in fts_rows}
                allowed: set[int] = set()
                ids = list(cand)
                for i in range(0, len(ids), 500):        # chunked IN() — SQLite var limit
                    batch = ids[i:i + 500]
                    ph = ",".join("?" * len(batch))
                    allowed.update(r[0] for r in con.execute(
                        f"SELECT id FROM chunks WHERE type=? AND id IN ({ph})",
                        [card_type, *batch]))
                vec_rows = [r for r in vec_rows if r[0] in allowed][: self.cfg.vec_pool]
                fts_rows = [r for r in fts_rows if r[0] in allowed][: self.cfg.fts_pool]
            # RRF fusion
            fused: dict[int, float] = {}
            for r, (cid,) in enumerate(vec_rows):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.cfg.rrf_k + r)
            for r, (cid,) in enumerate(fts_rows):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (self.cfg.rrf_k + r)
            if not fused:
                return []
            # temporal decay (evergreen exempt)
            # Age is computed NOW from the file's indexed mtime. chunks.age_days
            # is the age at INDEX time and freezes there: a card untouched since
            # the last rebuild looked exactly as young as it was that day (18
            # days stale fleet-wide on 2026-10-05), so freshly re-indexed cards
            # and old ones decayed on different clocks.
            if self.cfg.half_life_days > 0:
                lam = math.log(2) / self.cfg.half_life_days
                now = time.time()
                for cid in list(fused):
                    row = con.execute(
                        "SELECT c.age_days, c.evergreen, f.mtime FROM chunks c "
                        "LEFT JOIN files f ON f.path = c.path WHERE c.id=?", (cid,)).fetchone()
                    if row and not row[1]:
                        age = (max(0.0, (now - row[2]) / 86400.0)
                               if row[2] is not None and row[2] > 0 else row[0])
                        fused[cid] *= math.exp(-lam * age)
            ranked = sorted(fused, key=lambda c: -fused[c])
            # One hit per card: its best-ranked chunk. With token-window chunks
            # a long card yields several near-identical candidates, and a
            # recall for "ONNX crash" spent 3 of 10 slots on one benchmark card.
            # Agents want distinct cards; the file is the unit they open.
            paths: dict[int, str] = {}
            for i in range(0, len(ranked), 500):
                batch = ranked[i:i + 500]
                ph = ",".join("?" * len(batch))
                paths.update(con.execute(
                    f"SELECT id, path FROM chunks WHERE id IN ({ph})", batch))
            seen: set[str] = set()
            deduped = []
            by_path: dict[str, list[int]] = {}   # every candidate per card, best first
            for cid in ranked:
                p = paths.get(cid)
                by_path.setdefault(p, []).append(cid)
                if p in seen:
                    continue
                seen.add(p)
                deduped.append(cid)
            ranked = deduped
            # MMR diversity over the fused candidates
            selected = self._mmr(con, ranked, k)
            out = []
            for rid in selected:
                row = con.execute("SELECT cid,path,text,type FROM chunks WHERE id=?",
                                  (rid,)).fetchone()
                if row:
                    text = self._with_body(con, rid, row[1], row[2], by_path.get(row[1], []))
                    snip = text if self.cfg.snippet_chars <= 0 else text[: self.cfg.snippet_chars]
                    # Raw vector distance for the top hit, surfaced so callers
                    # (and the query log) have a real relevance signal. The RRF
                    # `score` is a pure rank reciprocal capped at ~2/rrf_k, so it
                    # says WHERE a hit ranked, never whether it is any good: over
                    # 616 logged recalls it spanned 0.016-0.033 whether the answer
                    # was correct or nonsense. `dist` is L2 on unnormalized model
                    # output, so it is comparable only WITHIN one tenant+backend —
                    # do not average it across the fleet. None when the chunk came
                    # from BM25 only and never entered the vector pool.
                    d = vec_dist.get(rid)
                    dist = round(d, 4) if d is not None else None
                    out.append({"id": row[0], "score": round(fused[rid], 5), "file": row[1],
                                "text": text, "snippet": snip, "type": row[3],
                                "dist": dist})
            if out:
                related = self._related(con, out[0]["file"])
                if related:
                    out[0]["related"] = related
            return out
        finally:
            con.close()

    _WHOLE_CARD_CHARS = 2000

    def _with_body(self, con, rid: int, path: str, text: str, candidates: list[int]) -> str:
        """The text to return for a card whose best chunk won the ranking.

        When that chunk is the card's summary (`name — description`), returning
        it alone hands the agent a title and hides the card: a recall for "why
        does the worker keep restarting" found the right card and the agent
        replied without its fix, because the fix was in the body. Append the
        card's best-ranked body chunk, or its first one if no body chunk made
        the candidate pool."""
        if not self.cfg.summary_chunk:
            return text
        try:
            with open(os.path.join(self.dir, path), encoding="utf-8", errors="ignore") as f:
                raw = f.read()
        except OSError:
            return text
        summary = card_summary(raw)
        if not summary or text.strip() != summary.strip():
            return text
        # Cards are meant to hold one fact: a short card goes back whole
        # (frontmatter dropped — the summary line already carries it).
        body = strip_frontmatter(raw).strip()
        if body and len(body) <= self._WHOLE_CARD_CHARS:
            return f"{text}\n{body}"
        body_id = next((c for c in candidates if c != rid), None)
        row = None
        if body_id is not None:
            row = con.execute("SELECT text FROM chunks WHERE id=?", (body_id,)).fetchone()
        if row is None:
            row = con.execute("SELECT text FROM chunks WHERE path=? AND id<>? AND text<>? "
                              "ORDER BY id LIMIT 1", (path, rid, text)).fetchone()
        return f"{text}\n{row[0]}" if row else text

    def _related(self, con, src_key: str, cap: int = 8) -> list[str]:
        """Resolve the [[wiki-links]] of one indexed file to card filenames that
        actually exist in the index. Ghost links (no such card) are dropped.

        A link resolves three ways, in order: the card's declared frontmatter
        `name:` slug, the filename stem, and finally either of those with `-`/`_`
        interchanged. Cards are written `project_foo_bar.md` but declare
        `name: project-foo-bar` and are linked as `[[project-foo-bar]]`, so
        filename-only matching found just 1 link in 3 on a real tenant — two
        thirds of the memory graph was invisible to recall."""
        out: list[str] = []
        for (target,) in con.execute(
                "SELECT DISTINCT target FROM links WHERE src_path=?", (src_key,)):
            row = None
            # Separator-insensitive, then type-prefix-insensitive (link_variants).
            for cand in link_variants(target):
                row = con.execute(
                    "SELECT path FROM files WHERE name = ? OR path = ? OR path LIKE ? "
                    "ORDER BY (name = ?) DESC LIMIT 1",
                    (cand, f"{cand}.md", f"%{os.sep}{cand}.md", cand)).fetchone()
                if row:
                    break
            if row and row[0] not in out:
                out.append(row[0])
                if len(out) >= cap:
                    break
        return out

    def diagnose(self) -> dict:
        """Report integrity problems that silently degrade recall.

        Every check here comes from a real loss found by hand-auditing a tenant:
        cards the index cannot type, links that point at nothing, cards written
        by an old MCP server under a timestamp filename, frontmatter nested
        inside frontmatter, and duplicate `name:` slugs (where a [[link]] can
        only ever reach one of them). Read-only — it never edits a card."""
        import collections
        con = self._connect()
        files = {key: fp for key, fp in self._iter_files()}
        report = {"tenant": self.dir, "files": len(files), "untyped": [],
                  "ghost_links": [], "legacy_names": [], "nested_frontmatter": [],
                  "duplicate_names": [], "no_frontmatter": []}

        names: dict[str, list[str]] = collections.defaultdict(list)
        resolvable: set[str] = set()
        # Any markdown file under the tenant is a real target, even when it is
        # deliberately kept out of the index (ignore_globs, archive/ in flat
        # mode): a link to project_state.md is not a ghost, just unindexed.
        for r, _ds, ns in os.walk(self.dir):
            for n in ns:
                if n.endswith(".md"):
                    st = n[:-3]
                    resolvable |= {st, st.replace("-", "_"), st.replace("_", "-")}
        for key, fp in sorted(files.items()):
            text = open(fp, encoding="utf-8", errors="ignore").read()
            base = os.path.basename(key)
            stem = base[:-3]
            nm = parse_frontmatter_name(text)
            if nm:
                names[nm].append(key)
                resolvable |= {nm, nm.replace("-", "_"), nm.replace("_", "-")}
            # basename, not the relative key: a link to an archived log
            # ([[2026-06-11]] → archive/2026-06-11.md) still resolves.
            resolvable |= {stem, stem.replace("-", "_"), stem.replace("_", "-")}

            # Dated files (YYYY-MM-DD.md) are session logs, not cards: they are
            # narrative by design and carry no frontmatter. Flagging them would
            # make doctor cry wolf on a healthy tenant.
            # MEMORY.md / INDEX.md are pointer indexes by convention, not cards
            # (repair() skips them for the same reason).
            is_log = bool(re.match(r"\d{4}-\d{2}-\d{2}$", stem))
            if not is_log and stem not in ("MEMORY", "INDEX"):
                if not _FRONTMATTER_RE.match(text):
                    report["no_frontmatter"].append(key)
                elif parse_frontmatter_type(text) is None:
                    report["untyped"].append(key)
            # A description whose value starts a second YAML block = the writer
            # wrapped an already-formatted card instead of passing it through.
            m = _FRONTMATTER_RE.match(text)
            if m and '"---' in m.group(0):
                report["nested_frontmatter"].append(key)
            if base.startswith("card-") or base.startswith("card_"):
                report["legacy_names"].append(key)

        for key, fp in sorted(files.items()):
            text = open(fp, encoding="utf-8", errors="ignore").read()
            for tgt in parse_wikilinks(text):
                if "/" in tgt or tgt.endswith(".md"):
                    continue          # a path to a file outside memory, not a card slug
                if not (set(link_variants(tgt)) & resolvable):
                    report["ghost_links"].append({"file": key, "target": tgt})

        report["duplicate_names"] = [{"name": n, "files": fs}
                                     for n, fs in sorted(names.items()) if len(fs) > 1]
        report["ok"] = not any(report[k] for k in
                               ("untyped", "ghost_links", "legacy_names",
                                "nested_frontmatter", "duplicate_names", "no_frontmatter"))
        con.close()
        return report

    # ---- mechanical card repair (the fixable half of diagnose()) -------------
    _CARD_TYPES = ("project", "feedback", "reference", "user")

    def repair(self, apply: bool = False) -> list[dict]:
        """Plan (and with apply=True, perform) mechanical fixes for cards that
        diagnose() flags and that need no judgement:

        - nested frontmatter: an old cogvault_record wrapped an already-formatted
          card in a second block; the outer one is dropped, the inner one kept.
        - missing type: inferred from the name/description/filename prefix, else
          `project` (every legacy MCP card audited on 2026-10-05 was one).
        - non-slug name ("card", a sentence, snake_case): rewritten as a slug.
        - timestamp filename (card-YYYYMMDD-…): renamed to <type>_<slug>.md.
        - no frontmatter on a <type>_*.md card: a minimal block is added.

        A rename rewrites every reference to the old filename/stem in the
        tenant (MEMORY.md pointers, [[links]]), so nothing is orphaned. Cards
        never overwrite each other; a taken name gets a numeric suffix.
        Returns one dict per touched card: {file, new_file, fixes}."""
        from .mcp_server import _slugify
        files = dict(self._iter_files())
        plan: list[dict] = []
        renames: dict[str, str] = {}
        taken = {os.path.basename(k) for k in files}
        for key, fp in sorted(files.items()):
            base = os.path.basename(key)
            stem = base[:-3]
            if re.match(r"\d{4}-\d{2}-\d{2}$", stem) or stem in ("MEMORY", "INDEX"):
                continue
            text = open(fp, encoding="utf-8", errors="ignore").read()
            fixes: list[str] = []
            m = _FRONTMATTER_RE.match(text)
            if m and '"---' in m.group(0):
                inner = text[m.end():].lstrip("\n")
                if _FRONTMATTER_RE.match(inner):
                    text, m = inner, _FRONTMATTER_RE.match(inner)
                    fixes.append("unwrap-nested")
            if not m:
                pref = stem.split("_", 1)[0]
                if pref not in self._CARD_TYPES:
                    continue                  # not a card by convention; leave it
                first = next((ln.strip("# ").strip() for ln in text.splitlines()
                              if ln.strip()), stem)
                text = ("---\n" f"name: {_slugify(stem)}\n"
                        f"description: {json_dumps(first[:150])}\n"
                        f"metadata:\n  type: {pref}\n---\n\n" + text.lstrip())
                m = _FRONTMATTER_RE.match(text)
                fixes.append("add-frontmatter")
            block = m.group(0)
            body = text[m.end():]
            name = parse_frontmatter_name(text) or ""
            ftype = parse_frontmatter_type(text)
            desc_m = re.search(r"^description:[ \t]*(.+)$", block, re.M)
            desc = desc_m.group(1).strip().strip("\"'") if desc_m else ""
            if ftype is None:
                # prefix only: "any word in the description" typed a Supabase
                # gotcha as `user` because its text mentioned users
                bare = re.sub(r"^card[-_]\d{8}[-_](\d{6}[-_])?", "", stem).lower()
                probes = (name.lower(), desc.lower(), bare)
                ftype = next((t for t in self._CARD_TYPES
                              if any(re.match(rf"{t}[-_:\s]", p) for p in probes)), "project")
                block = block.rstrip()[:-3].rstrip("\n") + f"\nmetadata:\n  type: {ftype}\n---\n"
                fixes.append(f"type={ftype}")
            # Only rewrite a name nothing can be linking to on purpose: empty,
            # the placeholder "card", or a sentence. A working slug (any
            # separator style) is what [[links]] target — renaming it orphans them.
            # Healthy cards keep their name even when it is a sentence: links
            # still reach them by filename stem, and rewriting them would only
            # churn files (and reset their decay clock) for no recall gain.
            legacy = base.startswith(("card-", "card_"))
            slug = name
            if (not name or name == "card"
                    or (legacy and not re.fullmatch(r"[\w.-]+", name))):
                src = name if name and name != "card" else ""
                if not src and not legacy:
                    src = stem               # convention: name = stem, dashed
                if not src:
                    src = next((ln.strip("# ").strip() for ln in body.splitlines()
                                if ln.strip()), "") or desc
                slug = _slugify(src)
                if slug and not slug.startswith(ftype + "-"):
                    slug = f"{ftype}-{slug}"
                if slug and slug != name:
                    if re.search(r"^name:.*$", block, re.M):
                        block = re.sub(r"^name:.*$", f"name: {slug}", block, count=1, flags=re.M)
                    else:
                        block = "---\n" + f"name: {slug}\n" + block[4:]
                    fixes.append(f"name={slug}")
            new_base = base
            if base.startswith(("card-", "card_")) and slug:
                want = _slugify(slug).replace("-", "_")
                if not want.startswith(ftype + "_"):
                    want = f"{ftype}_{want}"
                new_base, n = f"{want}.md", 2
                while new_base in taken:
                    new_base, n = f"{want}_{n}.md", n + 1
                taken.discard(base); taken.add(new_base)
                renames[base] = new_base
                fixes.append("rename")
            if fixes:
                plan.append({"file": key, "new_file": new_base, "fixes": fixes,
                             "_fp": fp, "_text": block + body})
        if apply:
            every: list[str] = []
            for item in plan:
                fp = item["_fp"]
                st = os.stat(fp)
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(item["_text"])
                # keep the card's age: a repair is not new information, and
                # temporal decay reads mtime
                os.utime(fp, (st.st_atime, st.st_mtime))
                if item["new_file"] != os.path.basename(fp):
                    os.rename(fp, os.path.join(os.path.dirname(fp), item["new_file"]))
            if renames:
                # Every .md under the tenant, NOT just indexed ones: MEMORY.md is
                # usually in ignore_globs, and it is exactly where pointers live.
                every = [os.path.join(r, n) for r, ds, ns in os.walk(self.dir)
                         for n in ns if n.endswith(".md")
                         if not any(part.startswith(".") for part in
                                    os.path.relpath(r, self.dir).split(os.sep) if part != ".")]
                for fp in every:
                    t = open(fp, encoding="utf-8", errors="ignore").read()
                    t2 = t
                    for old, new in renames.items():
                        t2 = t2.replace(old, new).replace(f"[[{old[:-3]}", f"[[{new[:-3]}")
                    if t2 != t:
                        st = os.stat(fp)
                        with open(fp, "w", encoding="utf-8") as f:
                            f.write(t2)
                        os.utime(fp, (st.st_atime, st.st_mtime))
            # mtime was preserved on purpose (decay), so the incremental
            # reindex cannot see these edits by mtime. Invalidate them in the
            # index instead (-1 = "re-read me", and not logged as an edit).
            touched = [os.path.relpath(it["_fp"], self.dir) for it in plan]
            touched += [os.path.relpath(fp, self.dir) for fp in every]
            con = self._connect()
            try:
                con.executemany("UPDATE files SET mtime=-1 WHERE path=?",
                                [(k,) for k in touched] +
                                [(os.path.basename(k),) for k in touched])
                con.commit()
            finally:
                con.close()
        for item in plan:
            item.pop("_fp", None); item.pop("_text", None)
        return plan

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
