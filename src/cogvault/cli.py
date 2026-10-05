"""cogvault CLI: index | search | mcp | stats | analyze | doctor | repair."""
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

    dr = sub.add_parser("doctor", help="Check a tenant for integrity problems "
                                        "that silently degrade recall")
    add_common(dr); dr.add_argument("--json", action="store_true")

    rp = sub.add_parser("repair", help="Fix the mechanical doctor findings "
                                       "(dry run unless --apply)")
    add_common(rp); rp.add_argument("--apply", action="store_true")

    ev = sub.add_parser("eval", help="Score recall against a judged query set "
                                     "(<tenant>/.cogvault-golden.jsonl)")
    add_common(ev)
    ev.add_argument("--golden", default=None,
                    help="JSONL of {query, relevant:[file,...]} (default: "
                         "<tenant>/.cogvault-golden.jsonl)")
    ev.add_argument("-k", type=int, default=5)
    ev.add_argument("--json", action="store_true")

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


def _doctor(v, as_json: bool) -> int:
    """Print a tenant's integrity report. Exit 1 when anything is wrong, so this
    can gate a pre-commit hook or a fleet sweep."""
    rep = v.diagnose()
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1

    print(f"cogvault doctor: {rep['files']} cards in {rep['tenant']}")
    if rep["ok"]:
        print("  ✓ no problems found")
        return 0

    def _section(key, title, hint, fmt=lambda x: f"      {x}"):
        rows = rep[key]
        if not rows:
            return
        print(f"\n  {title}: {len(rows)}")
        print(f"      {hint}")
        for r in rows[:15]:
            print(fmt(r))
        if len(rows) > 15:
            print(f"      … and {len(rows) - 15} more")

    _section("no_frontmatter", "cards with no frontmatter",
             "no name/description → recall relevance and [[links]] both suffer")
    _section("untyped", "cards with no type",
             "a `type` filter on recall will never match these")
    _section("legacy_names", "timestamp filenames",
             "written by cogvault < 0.10.0; rename to <type>_<slug>.md")
    _section("nested_frontmatter", "frontmatter inside frontmatter",
             "a formatted card got wrapped again — the inner name/type is lost")
    _section("duplicate_names", "duplicate name: slugs",
             "a [[link]] can only reach one of them",
             lambda r: f"      {r['name']}: {', '.join(r['files'])}")
    _section("ghost_links", "links pointing at nothing",
             "either the card was never written, or the slug is misspelled",
             lambda r: f"      {r['file']} → [[{r['target']}]]")
    return 1


def _run(a, v, cfg) -> int:
    if a.cmd == "doctor":
        return _doctor(v, a.json)
    if a.cmd == "eval":
        return _eval(a, v)
    if a.cmd == "repair":
        plan = v.repair(apply=a.apply)
        for it in plan:
            arrow = f" → {it['new_file']}" if it["new_file"] != os.path.basename(it["file"]) else ""
            print(f"  {it['file']}{arrow}\n      {', '.join(it['fixes'])}")
        print(f"\n{len(plan)} cards " + ("repaired." if a.apply else
              "would change (dry run; re-run with --apply)."))
        if a.apply and plan:
            print(json.dumps(v.reindex()))
        return 0
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


def _eval(a, v) -> int:
    """hit@1 / hit@k / MRR over a judged set of REAL queries. The set lives in
    the tenant (it is made of private queries) — never in this repo. Queries
    judged to have no answer in memory are counted as gaps, not scored."""
    os.environ["COGVAULT_LOG"] = "off"        # scoring must not pollute analyze
    path = a.golden or os.path.join(v.dir, ".cogvault-golden.jsonl")
    if not os.path.exists(path):
        print(f"no golden set at {path}", file=sys.stderr)
        return 2
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    scored, gaps, misses = [], 0, []
    for r in rows:
        rel = [os.path.basename(x) if not v.cfg.recursive else x
               for x in r.get("relevant") or []]
        if not rel:
            gaps += 1
            continue
        got = [x["file"] for x in v.search(r["query"], k=max(a.k, 10))]
        rank = next((i + 1 for i, f in enumerate(got) if f in rel), None)
        scored.append(rank)
        if rank != 1:
            misses.append((r["query"], rel[0], got[0] if got else None, rank))
    n = len(scored)
    rep = {"tenant": v.dir, "queries": len(rows), "scored": n, "gaps": gaps,
           "hit@1": round(sum(1 for x in scored if x == 1) / n, 3) if n else None,
           f"hit@{a.k}": round(sum(1 for x in scored if x and x <= a.k) / n, 3) if n else None,
           "mrr@10": round(sum(1 / x for x in scored if x) / n, 3) if n else None}
    if a.json:
        print(json.dumps({**rep, "misses": misses}, ensure_ascii=False, indent=2))
        return 0
    print(f"cogvault eval: {rep['tenant']}  ({n} scored, {gaps} gaps)")
    print(f"  hit@1 {rep['hit@1']}   hit@{a.k} {rep[f'hit@{a.k}']}   MRR@10 {rep['mrr@10']}")
    for q, want, top, rank in misses[:10]:
        print(f"  · rank {rank or '>10'}: {q[:50]!r}  want {want}  got {top}")
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
    # Cold recalls include loading the model (one-shot CLI processes); mixing
    # them in made a 15 ms search look like 530 ms. Rows logged before the
    # flag existed have no `cold` key and count as warm.
    lat = [r["latency_ms"] for r in rows
           if r.get("latency_ms") is not None and not r.get("cold")]
    lat_cold = [r["latency_ms"] for r in rows
                if r.get("latency_ms") is not None and r.get("cold")]
    tops = [r["top_score"] for r in rows if r.get("top_score") is not None]
    # Vector distance is the only logged value that tracks relevance, but it is
    # comparable only WITHIN a tenant+backend (different models, and even the
    # same model across a fastembed pooling change, live on different scales).
    # So report a per-tenant p50/p90 rather than one meaningless fleet average,
    # and flag each tenant's own worst decile as "weak" — those queries are where
    # recall returned something but probably not the right thing.
    dists_by_tenant: dict[str, list[float]] = {}
    for r in rows:
        d = r.get("top_dist")
        if d is not None:
            dists_by_tenant.setdefault(r.get("tenant", "?"), []).append(d)
    weak_queries: list[tuple[str, float, str]] = []
    for r in rows:
        d, t = r.get("top_dist"), r.get("tenant", "?")
        pool = dists_by_tenant.get(t) or []
        if d is None or len(pool) < 10:
            continue
        p90 = sorted(pool)[int(len(pool) * 0.9)]
        if d >= p90:                      # larger distance = worse match
            weak_queries.append((t, d, r.get("query", "")))
    by_tenant: dict[str, int] = {}
    for r in rows:
        by_tenant[r.get("tenant", "?")] = by_tenant.get(r.get("tenant", "?"), 0) + 1

    # Count NEW cards per tenant, not every touch: tenants that rewrite a running
    # project_state.md daily would otherwise drown out the signal ("how many facts
    # did this agent actually save"). Updates/deletes are reported separately.
    writes_by_tenant: dict[str, int] = {}
    ops: dict[str, int] = {}
    for r in wrows:
        op = r.get("op", "create")       # pre-0.9.0 records had no op; all were creates
        ops[op] = ops.get(op, 0) + 1
        if op == "create":
            t = r.get("tenant", "?")
            writes_by_tenant[t] = writes_by_tenant.get(t, 0) + 1

    err_by_type: dict[str, int] = {}
    for r in erows:
        k = r.get("error", "?")
        err_by_type[k] = err_by_type.get(k, 0) + 1

    if a.json:
        print(json.dumps({
            "recalls": n, "empty_rate": round(empties / n, 3) if n else None,
            "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
            "latency_p95_ms": round(sorted(lat)[int(len(lat) * 0.95)], 1) if len(lat) > 2 else None,
            "cold_latency_p50_ms": round(statistics.median(lat_cold), 1) if lat_cold else None,
            "cold_recalls": len(lat_cold),
            # avg_top_score is retained for continuity but is NOT a quality
            # signal: RRF scores are rank reciprocals capped at 2/rrf_k.
            "avg_top_score": round(statistics.mean(tops), 5) if tops else None,
            "dist_by_tenant": {t: {"p50": round(statistics.median(v), 3),
                                   "p90": round(sorted(v)[int(len(v) * 0.9)], 3),
                                   "n": len(v)}
                               for t, v in sorted(dists_by_tenant.items())},
            "weak_hits": len(weak_queries),
            "by_tenant": by_tenant,
            "records": len(wrows), "records_by_tenant": writes_by_tenant,
            "records_by_op": ops,
            "errors": len(erows),
            "errors_by_type": err_by_type}, indent=2))
        return 0

    print(f"cogvault — recall effectiveness  ({path})\n")
    print(f"  recalls         {n}")
    if n:
        # A hybrid search returns the top-k of its candidate pool, so a truly
        # empty result only happens on an empty tenant — this rate is ~always 0
        # and must NOT be read as "recall is healthy". The weak-hit list below
        # is the real signal for a query that found nothing useful.
        print(f"  empty results   {empties}/{n} ({empties/n:.0%})   "
              f"← only ever non-zero on an empty index")
    if lat:
        print(f"  latency p50/p95 {statistics.median(lat):.0f} / "
              f"{sorted(lat)[int(len(lat)*0.95)] if len(lat)>2 else lat[-1]:.0f} ms")
    if lat_cold:
        print(f"  cold-start p50  {statistics.median(lat_cold):.0f} ms  "
              f"({len(lat_cold)} recalls paid a model load)")
    if dists_by_tenant:
        print("  top-hit distance (lower = better; per tenant, not comparable across)")
        for t, v in sorted(dists_by_tenant.items(), key=lambda x: -len(x[1])):
            if len(v) < 3:            # too few samples for a p50/p90 to mean anything
                continue
            print(f"    {t:28} p50={statistics.median(v):6.3f} "
                  f"p90={sorted(v)[int(len(v)*0.9)]:6.3f}  n={len(v)}")
    if by_tenant:
        print(f"  by tenant       " + ", ".join(f"{t}:{c}" for t, c in
              sorted(by_tenant.items(), key=lambda x: -x[1])))
    # Read-only tenants are the actionable half of this line: agents that never
    # record are accumulating unwritten lessons.
    new_cards = ops.get("create", 0)
    print(f"  new cards       {new_cards}" + ("  (" + ", ".join(
        f"{t}:{c}" for t, c in sorted(writes_by_tenant.items(), key=lambda x: -x[1])) + ")"
        if writes_by_tenant else ""))
    if ops.get("update") or ops.get("delete"):
        print(f"  card edits      {ops.get('update', 0)} updated, "
              f"{ops.get('delete', 0)} deleted")
    # Failures rank above no-hits: a no-hit is a memory gap, an error is broken
    # plumbing, and a silently broken tenant looks exactly like a forgetful agent.
    if erows:
        print(f"  errors          {len(erows)}  (" + ", ".join(
            f"{t}:{c}" for t, c in sorted(err_by_type.items(), key=lambda x: -x[1])) + ")"
            + "   ← investigate")
        for r in erows[-5:]:
            print(f"    · {r.get('tenant','?')} {r.get('op','?')}: "
                  f"{r.get('error','?')}: {str(r.get('message',''))[:60]}")
    if weak_queries:
        print("\n  weakest hits (worst decile by vector distance — recall likely missed):")
        for t, d, q in sorted(weak_queries, key=lambda x: -x[1])[:8]:
            print(f"    · [{t}] dist={d:.3f}  {q[:58]}")
    # surface recent no-hit queries — these are the actionable signal
    misses = [r["query"] for r in rows if r.get("empty")][-8:]
    if misses:
        print("\n  recent no-hit queries (memory may be missing these):")
        for q in misses:
            print(f"    · {q[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
