"""
cogvault.mcp_server — stdio MCP server exposing recall/record over one tenant vault.

Run:  cogvault mcp --tenant ~/agent/memory
Tools: cogvault_recall (hybrid search), cogvault_record (append a markdown card).
"""
from __future__ import annotations
import sys, json, os, datetime
from .core import Vault, Config
from .obs import log_error
from . import __version__

PROTOCOL = "2024-11-05"


_TYPES = ("project", "feedback", "reference", "user")


def _slugify(s: str, limit: int = 60) -> str:
    """kebab-case slug from a title: lowercase, non-alphanumerics collapsed to a
    single dash. Non-ASCII letters are kept (tenants write Ukrainian titles)."""
    out, prev_dash = [], False
    for c in s.lower().strip():
        if c.isalnum():
            out.append(c); prev_dash = False
        elif not prev_dash:
            out.append("-"); prev_dash = True
    return "".join(out).strip("-")[:limit]


def _write_card(tenant_dir: str, content: str, title: str | None = None,
                card_type: str | None = None) -> str:
    """Append a memory as a real markdown file (source of truth).

    Two failure modes this function used to have, both of which silently
    degraded tenants until someone audited them by hand:

    1. Filenames were `card-<timestamp>-<40 chars of content>.md`. Those sort by
       write time, collide with nothing, and tell a human nothing — and because
       the convention everywhere else is `<type>_<slug>.md`, they were invisible
       to anyone grepping the vault by topic. Now a card is named
       `<type>_<slug>.md` from its title, falling back to a timestamp ONLY when
       there is no usable title.
    2. `content` that already carried frontmatter got a SECOND frontmatter block
       wrapped around it, with the inner block's raw YAML quoted into the outer
       `description:`. parse_frontmatter_type then read the outer block, so the
       card's real type and name were lost. Now pre-formatted content is written
       through untouched (its own frontmatter completed if incomplete).

    Cards never overwrite: a name collision gets a numeric suffix."""
    text = content.strip()
    has_fm = text.startswith("---\n") or text.startswith("---\r\n")

    if has_fm:
        # Caller handed us a formatted card. Respect it: read its own name/type
        # rather than wrapping a second block around it.
        from .core import parse_frontmatter_name, parse_frontmatter_type, _FRONTMATTER_RE
        name = parse_frontmatter_name(text) or _slugify(title or "") or None
        ftype = parse_frontmatter_type(text) or (card_type or "").strip().lower() or None
        m = _FRONTMATTER_RE.match(text)
        block = m.group(0).split("---", 2)[1].strip("\n")
        body = text[m.end():].lstrip("\n")
        lines = [ln for ln in block.splitlines()]
        if name and not any(ln.startswith("name:") for ln in lines):
            lines.insert(0, f"name: {json.dumps(name, ensure_ascii=False)}")
        if ftype and parse_frontmatter_type(text) is None:
            lines.append("metadata:"); lines.append(f"  type: {ftype}")
        out_text = "---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n"
    else:
        name = _slugify(title) if title else ""
        ftype = (card_type or "").strip().lower()
        if ftype and ftype not in _TYPES:
            ftype = ""
        # A type prefix in the title ("project: X") is the type, not part of the name.
        if not ftype:
            for cand in _TYPES:
                if name.startswith(cand + "-"):
                    ftype, name = cand, name[len(cand) + 1:]
                    break
        display = " ".join((title or name.replace("-", " ")).split()) or "card"
        desc = " ".join(content.split())[:150]
        slug = f"{ftype}-{name}" if ftype and name else (name or "")
        fm = [f"name: {json.dumps(slug or display, ensure_ascii=False)}",
              f"description: {json.dumps(desc, ensure_ascii=False)}"]
        if ftype:
            fm += ["metadata:", f"  type: {ftype}"]
        out_text = "---\n" + "\n".join(fm) + "\n---\n\n" + text + "\n"
        name = slug or name

    # Filename: <type>_<slug>.md, matching every hand-written card in the fleet.
    stem = (name or "").replace("-", "_").strip("_")
    if stem and ftype and not stem.startswith(ftype + "_"):
        stem = f"{ftype}_{stem}"
    if not stem:
        # No title and no frontmatter name — nothing meaningful to name it after.
        stem = "card_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    fp = os.path.join(tenant_dir, f"{stem}.md")
    n = 2
    while os.path.exists(fp):
        fp = os.path.join(tenant_dir, f"{stem}_{n}.md")
        n += 1
    with open(fp, "w", encoding="utf-8") as f:
        f.write(out_text)
    return fp


class MCPServer:
    def __init__(self, tenant_dir: str, cfg: Config):
        self.vault = Vault(tenant_dir, cfg)
        self.tenant_dir = tenant_dir
        self.tools = {
            "cogvault_recall": {
                "description": "Search this agent's persistent memory. Pass a natural-language "
                               "query; returns the most relevant memory snippets (hybrid "
                               "semantic + keyword).",
                "inputSchema": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "What to recall"},
                    "limit": {"type": "integer", "default": 5},
                    "type": {"type": "string", "description":
                             "Only recall cards of this frontmatter type "
                             "(e.g. user, feedback, project, reference)"}},
                    "required": ["query"]},
            },
            "cogvault_record": {
                "description": "Save a fact to persistent memory as a Markdown card. "
                               "It becomes searchable on the next recall. Pass `type` "
                               "so the card can be filtered on recall, and `title` so "
                               "it gets a meaningful filename.",
                "inputSchema": {"type": "object", "properties": {
                    "content": {"type": "string", "description": "The fact to remember"},
                    "title": {"type": "string", "description":
                              "Short title — becomes the card's name and filename"},
                    "type": {"type": "string", "enum": list(_TYPES),
                             "description": "Card type: project (ongoing work/decision), "
                                            "feedback (a rule or correction), reference "
                                            "(pointer to a resource), user (who the user is)"}},
                    "required": ["content"]},
            },
        }

    def handle(self, req: dict) -> dict | None:
        m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            return self._ok(rid, {"protocolVersion": PROTOCOL,
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "cogvault", "version": __version__}})
        if m == "ping":
            return self._ok(rid, {})
        if isinstance(m, str) and m.startswith("notifications/"):
            return None               # JSON-RPC: never respond to notifications
        if m == "tools/list":
            return self._ok(rid, {"tools": [
                {"name": n, **spec} for n, spec in self.tools.items()]})
        if m == "tools/call":
            p = req.get("params", {})
            name = p.get("name"); args = p.get("arguments", {})
            try:
                if name == "cogvault_recall":
                    res = self.vault.search(args["query"], k=args.get("limit", 5),
                                            card_type=args.get("type"))
                    if not res:
                        # Close the loop on memory gaps: a no-hit recall is the
                        # exact moment the agent knows a card is missing.
                        text = ("No matching memories found. If you end up solving "
                                "this, save the durable part with cogvault_record "
                                "so the next recall lands.")
                    else:
                        def _block(r):
                            b = f"[{r['score']}] {r['file']}\n{r['text']}"
                            if r.get("related"):
                                b += f"\nRelated: {', '.join(r['related'])}"
                            return b
                        text = "\n\n".join(_block(r) for r in res)
                    return self._ok(rid, {"content": [{"type": "text", "text": text}]})
                if name == "cogvault_record":
                    # Validate before writing. A missing/blank `content` used to
                    # raise KeyError deep inside _write_card, which reached the
                    # agent as an opaque JSON-RPC -32000 "'content'" — the agent
                    # believed it had saved a memory that was never written (one
                    # such silent loss on tenant-d, 2026-07). Fail loudly with
                    # an invalid-params error naming the field instead.
                    content = args.get("content") if isinstance(args, dict) else None
                    if not isinstance(content, str) or not content.strip():
                        return self._err(rid, -32602,
                            "cogvault_record requires a non-empty string 'content' "
                            "(the fact to remember); nothing was written.")
                    fp = _write_card(self.tenant_dir, content, args.get("title"),
                                     args.get("type"))
                    # reindex() logs the `record` event itself (it is the one
                    # place that sees BOTH this tool and the far more common
                    # write-a-file-then-`cogvault index` path). Logging here too
                    # would double-count every MCP write.
                    self.vault.reindex()
                    return self._ok(rid, {"content": [{"type": "text",
                            "text": json.dumps({"status": "ok", "file": os.path.basename(fp)})}]})
                return self._err(rid, -32601, f"unknown tool {name}")
            except Exception as e:
                log_error(self.tenant_dir, name or "call", e,
                          query=args.get("query") if isinstance(args, dict) else None)
                return self._err(rid, -32000, str(e))
        if rid is None:
            return None               # unknown id-less message = notification: stay silent
        return self._err(rid, -32601, f"unknown method {m}")

    @staticmethod
    def _ok(rid, result): return {"jsonrpc": "2.0", "id": rid, "result": result}
    @staticmethod
    def _err(rid, code, msg): return {"jsonrpc": "2.0", "id": rid,
                                      "error": {"code": code, "message": msg}}


def serve(tenant_dir: str, cfg: Config):
    srv = MCPServer(tenant_dir, cfg)
    srv.vault.reindex()  # warm index on boot
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = srv.handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
