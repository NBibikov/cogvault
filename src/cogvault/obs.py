"""
cogvault.obs — lightweight JSONL query log for effectiveness analysis.

One append-only line per recall/record, written to ~/.cache/cogvault/query-log.jsonl
(override with COGVAULT_LOG, or "" / "off" to disable). No external deps, no PII
beyond the query text the agent already sees. Used by `cogvault analyze`.
"""
from __future__ import annotations
import os, json, time


def tenant_label(tenant: str) -> str:
    """Human-readable tenant label for the log: last two path components.
    A bare basename is useless fleet-wide — every agent's tenant is
    `~/Agents/<name>/memory`, so they'd all collapse into "memory"."""
    parts = tenant.rstrip("/").split(os.sep)
    return "/".join(p for p in parts[-2:] if p) or tenant

def _log_path() -> str | None:
    v = os.environ.get("COGVAULT_LOG")
    if v in ("off", "0", "false"):
        return None
    if v:
        return os.path.expanduser(v)
    cache = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "cogvault")
    return os.path.join(cache, "query-log.jsonl")

# Rotate past this size (keep one .1 generation) — the log is append-only and
# would otherwise grow unbounded across the fleet's lifetime.
MAX_LOG_BYTES = 5_000_000

def _append(rec: dict):
    """Append one event line. Best-effort: observability never raises into callers."""
    path = _log_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > MAX_LOG_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass                       # no log yet, or a concurrent rotate won
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def log_recall(tenant: str, query: str, results: list, latency_ms: float,
               ts: float | None = None, card_type: str | None = None,
               cold: bool | None = None):
    """Append one structured recall event."""
    top = results[0] if results else None
    _append({
        "ts": round(ts if ts is not None else time.time(), 3),
        "event": "recall",
        "tenant": tenant_label(tenant),
        "query": query,
        **({"type": card_type} if card_type else {}),
        "n_results": len(results),
        # `top_score` is the RRF rank-reciprocal: useful for debugging fusion,
        # useless as relevance. Its whole observed range across 616 logged
        # recalls was 0.016-0.033 (theoretical cap 2/rrf_k), identical for a
        # correct hit and for nonsense. `top_dist` is the raw vector distance
        # for the same hit — the only value here that actually tracks whether
        # the answer was any good. Compare it only within one tenant+backend.
        "top_score": top["score"] if top else None,
        "top_dist": top.get("dist") if top else None,
        "top_file": top["file"] if top else None,
        "scores": [r["score"] for r in results[:5]],
        "dists": [r.get("dist") for r in results[:5]],
        "latency_ms": round(latency_ms, 1),
        **({"cold": cold} if cold is not None else {}),
        "empty": not results,
    })

def log_error(tenant: str, op: str, exc: BaseException, ts: float | None = None,
              query: str | None = None):
    """Append one failure event. Without this the fleet is blind to breakage:
    a recall that raises reaches the agent as an MCP error and vanishes, so the
    only symptom is an agent that mysteriously "forgot" something. `analyze`
    surfaces these; keep the message short — the type is the actionable part."""
    _append({
        "ts": round(ts if ts is not None else time.time(), 3),
        "event": "error",
        "tenant": tenant_label(tenant),
        "op": op,
        "error": type(exc).__name__,
        "message": str(exc)[:500],
        **({"query": query} if query else {}),
    })


def log_record(tenant: str, file: str, content: str, ts: float | None = None,
               op: str = "create", chunks: int | None = None):
    """Append one write event — `analyze` pairs these with recalls to show which
    agents actually write memory vs only read it.

    Two callers, deliberately: the MCP `cogvault_record` tool (which has the card
    text, hence `chars`), and `Vault.reindex()` (which does not, but sees every
    card written directly to disk). The second path matters more in practice —
    agents and the /remember skill write markdown files and then run
    `cogvault index`, so before it existed the log showed 616 recalls against 24
    records and reported busy tenants as read-only.

    `op` distinguishes a brand-new card (`create`) from a revision of an existing
    one (`update`) or a card removed from disk (`delete`), so "how many NEW facts
    did this agent save" stays answerable even on tenants that rewrite a running
    project_state.md every day.
    """
    _append({
        "ts": round(ts if ts is not None else time.time(), 3),
        "event": "record",
        "tenant": tenant_label(tenant),
        "file": file,
        "op": op,
        **({"chunks": chunks} if chunks is not None else {}),
        "chars": len(content),
    })
