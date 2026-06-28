"""cogvault CLI: index | search | mcp | stats | analyze."""
from __future__ import annotations
import argparse, json, os, sys, statistics
from .core import Vault, Config
from . import __version__


def main(argv=None):
    p = argparse.ArgumentParser(prog="cogvault",
        description="Fleet-grade local memory over plain Markdown.")
    p.add_argument("--version", action="version", version=f"cogvault {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--tenant", required=True, help="Tenant memory directory")
        sp.add_argument("--half-life", type=float, default=0.0,
                        help="Temporal decay half-life in days (0=off)")
        sp.add_argument("--mmr", type=float, default=0.7, help="MMR lambda (1=relevance)")
        # source options — for vaults with a folder tree (e.g. Obsidian)
        sp.add_argument("--recursive", action="store_true",
                        help="Walk subdirectories (e.g. an Obsidian vault)")
        sp.add_argument("--strip-frontmatter", action="store_true",
                        help="Drop a leading YAML frontmatter block before indexing")
        sp.add_argument("--ignore", action="append", default=[], metavar="GLOB",
                        help="Path glob to skip, relative to tenant (repeatable), "
                             'e.g. --ignore ".obsidian/*" --ignore "Templates/*"')

    ix = sub.add_parser("index", help="(Re)build the index from markdown files")
    add_common(ix)

    se = sub.add_parser("search", help="Hybrid search")
    add_common(se); se.add_argument("query"); se.add_argument("-k", type=int, default=5)
    se.add_argument("--json", action="store_true")

    mc = sub.add_parser("mcp", help="Run stdio MCP server for this tenant")
    add_common(mc)

    st = sub.add_parser("stats", help="Index stats")
    st.add_argument("--tenant", required=True)

    an = sub.add_parser("analyze", help="Effectiveness report from the query log")
    an.add_argument("--tenant", help="Filter to one tenant (default: all)")
    an.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help(); return 1

    if a.cmd == "analyze":
        return _analyze(a)

    if a.cmd == "stats":
        v = Vault(a.tenant)
        con = v._connect()
        n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        files = con.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
        con.close()
        print(f"cogvault: {files} files, {n} chunks indexed at {v.db_path}")
        return 0

    cfg = Config(half_life_days=a.half_life, mmr_lambda=a.mmr,
                 recursive=getattr(a, "recursive", False),
                 strip_frontmatter=getattr(a, "strip_frontmatter", False),
                 ignore_globs=tuple(getattr(a, "ignore", []) or ()))
    v = Vault(a.tenant, cfg)

    if a.cmd == "index":
        print(json.dumps(v.reindex()))
    elif a.cmd == "search":
        res = v.search(a.query, k=a.k)
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            for r in res:
                print(f"  {r['score']:>8}  {r['file']}")
                print(f"            {r['snippet'][:100]}")
    elif a.cmd == "mcp":
        from .mcp_server import serve
        serve(a.tenant, cfg)
    return 0


def _analyze(a) -> int:
    """Read the JSONL query log and print an effectiveness report."""
    from .obs import _log_path
    path = _log_path()
    if not path or not os.path.exists(path):
        print("No query log yet. Run some recalls first (log: "
              f"{path or 'disabled'}).")
        return 0
    rows = []
    tfilter = os.path.basename(a.tenant.rstrip("/")) if a.tenant else None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event") != "recall":
                continue
            if tfilter and d.get("tenant") != tfilter:
                continue
            rows.append(d)
    if not rows:
        print("No recall events match.")
        return 0
    n = len(rows)
    empties = sum(1 for r in rows if r.get("empty"))
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    tops = [r["top_score"] for r in rows if r.get("top_score") is not None]
    by_tenant: dict[str, int] = {}
    for r in rows:
        by_tenant[r.get("tenant", "?")] = by_tenant.get(r.get("tenant", "?"), 0) + 1

    if a.json:
        print(json.dumps({
            "recalls": n, "empty_rate": round(empties / n, 3),
            "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
            "latency_p95_ms": round(sorted(lat)[int(len(lat) * 0.95)], 1) if len(lat) > 2 else None,
            "avg_top_score": round(statistics.mean(tops), 5) if tops else None,
            "by_tenant": by_tenant}, indent=2))
        return 0

    print(f"cogvault — recall effectiveness  ({path})\n")
    print(f"  recalls         {n}")
    print(f"  no-hit rate     {empties}/{n} ({empties/n:.0%})   "
          f"← high = memory gaps or query mismatch")
    if lat:
        print(f"  latency p50/p95 {statistics.median(lat):.0f} / "
              f"{sorted(lat)[int(len(lat)*0.95)] if len(lat)>2 else lat[-1]:.0f} ms")
    if tops:
        print(f"  avg top score   {statistics.mean(tops):.4f}")
    print(f"  by tenant       " + ", ".join(f"{t}:{c}" for t, c in
          sorted(by_tenant.items(), key=lambda x: -x[1])))
    # surface recent no-hit queries — these are the actionable signal
    misses = [r["query"] for r in rows if r.get("empty")][-8:]
    if misses:
        print("\n  recent no-hit queries (memory may be missing these):")
        for q in misses:
            print(f"    · {q[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
