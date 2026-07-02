"""
cogvault.obs — lightweight JSONL query log for effectiveness analysis.

One append-only line per recall, written to ~/.cache/cogvault/query-log.jsonl
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

def log_recall(tenant: str, query: str, results: list, latency_ms: float,
               ts: float | None = None, card_type: str | None = None):
    """Append one structured recall event. Best-effort: never raises into search()."""
    path = _log_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        top = results[0] if results else None
        rec = {
            "ts": round(ts if ts is not None else time.time(), 3),
            "event": "recall",
            "tenant": tenant_label(tenant),
            "query": query,
            **({"type": card_type} if card_type else {}),
            "n_results": len(results),
            "top_score": top["score"] if top else None,
            "top_file": top["file"] if top else None,
            "scores": [r["score"] for r in results[:5]],
            "latency_ms": round(latency_ms, 1),
            "empty": not results,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass   # observability must never break recall
