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
        sp.add_argument("--model", default=None,
                        help="Embedding model (overrides .cogvault.toml and "
                             "$COGVAULT_MODEL). Must match the model the index was "
                             "built with, or the index is fully re-embedded.")
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
    se.add_argument("--type", default=None,
                    help="Only cards whose frontmatter type matches "
                         "(e.g. user, feedback, project, reference)")

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

    # Resolve config in explicit precedence: CLI flags > $COGVAULT_MODEL > .cogvault.toml
    # > defaults. We start from defaults, layer the tenant file (which is why we pass a
    # Config to Vault — so Vault won't re-apply the file), then stamp explicit flags last.
    from .core import apply_tenant_config
    cfg = Config()
    explicit: set[str] = set()
    flag_map = {                                  # CLI flag → (Config field, argparse default)
        "model": ("model", None), "half_life": ("half_life_days", 0.0),
        "mmr": ("mmr_lambda", 0.7), "recursive": ("recursive", False),
        "strip_frontmatter": ("strip_frontmatter", False),
    }
    for flag, (field, default) in flag_map.items():
        if getattr(a, flag, default) != default:
            explicit.add(field)
    if getattr(a, "ignore", None):
        explicit.add("ignore_globs")
    # Layer the tenant file onto fields the operator did NOT set explicitly.
    apply_tenant_config(cfg, a.tenant, respect_env=True, skip=explicit)
    # Now stamp the explicit flags so they win over the file.
    if getattr(a, "model", None):       cfg.model = a.model
    if a.half_life != 0.0:              cfg.half_life_days = a.half_life
    if a.mmr != 0.7:                    cfg.mmr_lambda = a.mmr
    if getattr(a, "recursive", False): cfg.recursive = True
    if getattr(a, "strip_frontmatter", False): cfg.strip_frontmatter = True
    if getattr(a, "ignore", None):     cfg.ignore_globs = tuple(a.ignore)
    v = Vault(a.tenant, cfg)

    try:
        return _run(a, v, cfg)
    except Exception as e:
        # Log before re-raising: a subagent's failed CLI recall would otherwise
        # leave no trace anywhere, and the only symptom is a "forgetful" agent.
        from .obs import log_error
        log_error(a.tenant, a.cmd, e, query=getattr(a, "query", None))
        raise


def _run(a, v, cfg) -> int:
    if a.cmd == "index":
        print(json.dumps(v.reindex()))
    elif a.cmd == "search":
        res = v.search(a.query, k=a.k, card_type=a.type)
        if not res and v.join_heal(timeout=600):
            # a corrupt index triggered a background rebuild — in a short-lived
            # CLI process the daemon thread would die at exit, leaving the index
            # broken and every future CLI call empty. Wait it out and retry once.
            res = v.search(a.query, k=a.k, card_type=a.type)
        if not res:
            # stderr so --json stdout stays parseable; same gap-closing nudge
            # the MCP server gives — subagents hit this path via the CLI.
            print("cogvault: no hits — if you solve this, record a card so the "
                  "next recall lands.", file=sys.stderr)
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            for r in res:
                print(f"  {r['score']:>8}  {r['file']}")
                print(f"            {r['snippet'][:100]}")
                if r.get("related"):
                    print(f"            related: {', '.join(r['related'])}")
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
    rows, wrows, erows = [], [], []
    tfilter = None
    if a.tenant:
        from .obs import tenant_label
        # match both the current parent/basename label and the legacy bare basename
        tfilter = {tenant_label(a.tenant), os.path.basename(a.tenant.rstrip("/"))}
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if tfilter and d.get("tenant") not in tfilter:
                continue
            if d.get("event") == "recall":
                rows.append(d)
            elif d.get("event") == "record":
                wrows.append(d)
            elif d.get("event") == "error":
                erows.append(d)
    if not rows and not wrows and not erows:
        print("No events match.")
        return 0
    n = len(rows)
    empties = sum(1 for r in rows if r.get("empty"))
    lat = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    tops = [r["top_score"] for r in rows if r.get("top_score") is not None]
    by_tenant: dict[str, int] = {}
    for r in rows:
        by_tenant[r.get("tenant", "?")] = by_tenant.get(r.get("tenant", "?"), 0) + 1

    writes_by_tenant: dict[str, int] = {}
    for r in wrows:
        writes_by_tenant[r.get("tenant", "?")] = writes_by_tenant.get(r.get("tenant", "?"), 0) + 1

    err_by_type: dict[str, int] = {}
    for r in erows:
        k = r.get("error", "?")
        err_by_type[k] = err_by_type.get(k, 0) + 1

    if a.json:
        print(json.dumps({
            "recalls": n, "empty_rate": round(empties / n, 3) if n else None,
            "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
            "latency_p95_ms": round(sorted(lat)[int(len(lat) * 0.95)], 1) if len(lat) > 2 else None,
            "avg_top_score": round(statistics.mean(tops), 5) if tops else None,
            "by_tenant": by_tenant,
            "records": len(wrows), "records_by_tenant": writes_by_tenant,
            "errors": len(erows),
            "errors_by_type": err_by_type}, indent=2))
        return 0

    print(f"cogvault — recall effectiveness  ({path})\n")
    print(f"  recalls         {n}")
    if n:
        print(f"  no-hit rate     {empties}/{n} ({empties/n:.0%})   "
              f"← high = memory gaps or query mismatch")
    if lat:
        print(f"  latency p50/p95 {statistics.median(lat):.0f} / "
              f"{sorted(lat)[int(len(lat)*0.95)] if len(lat)>2 else lat[-1]:.0f} ms")
    if tops:
        print(f"  avg top score   {statistics.mean(tops):.4f}")
    if by_tenant:
        print(f"  by tenant       " + ", ".join(f"{t}:{c}" for t, c in
              sorted(by_tenant.items(), key=lambda x: -x[1])))
    # Read-only tenants are the actionable half of this line: agents that never
    # record are accumulating unwritten lessons.
    print(f"  records         {len(wrows)}" + ("  (" + ", ".join(
        f"{t}:{c}" for t, c in sorted(writes_by_tenant.items(), key=lambda x: -x[1])) + ")"
        if writes_by_tenant else ""))
    # Failures rank above no-hits: a no-hit is a memory gap, an error is broken
    # plumbing, and a silently broken tenant looks exactly like a forgetful agent.
    if erows:
        print(f"  errors          {len(erows)}  (" + ", ".join(
            f"{t}:{c}" for t, c in sorted(err_by_type.items(), key=lambda x: -x[1])) + ")"
            + "   ← investigate")
        for r in erows[-5:]:
            print(f"    · {r.get('tenant','?')} {r.get('op','?')}: "
                  f"{r.get('error','?')}: {str(r.get('message',''))[:60]}")
    # surface recent no-hit queries — these are the actionable signal
    misses = [r["query"] for r in rows if r.get("empty")][-8:]
    if misses:
        print("\n  recent no-hit queries (memory may be missing these):")
        for q in misses:
            print(f"    · {q[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
