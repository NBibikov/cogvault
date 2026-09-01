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


def _write_card(tenant_dir: str, content: str, title: str | None = None) -> str:
    """Append a memory as a real markdown file (source of truth). Cards carry
    the same name/description frontmatter as hand-written ones, and filenames
    never overwrite: same-second records get a numeric suffix."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = (title or content[:40]).lower()
    slug = "".join(c if c.isalnum() else "-" for c in slug).strip("-")[:50] or "card"
    fp = os.path.join(tenant_dir, f"card-{ts}-{slug}.md")
    n = 2
    while os.path.exists(fp):
        fp = os.path.join(tenant_dir, f"card-{ts}-{slug}-{n}.md")
        n += 1
    name = " ".join((title or slug.replace("-", " ")).split())
    desc = " ".join(content.split())[:150]
    # json.dumps → safely quoted YAML scalars (titles may contain quotes/colons)
    body = (f"---\nname: {json.dumps(name, ensure_ascii=False)}\n"
            f"description: {json.dumps(desc, ensure_ascii=False)}\n"
            f"---\n\n{content}\n")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(body)
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
                               "It becomes searchable on the next recall.",
                "inputSchema": {"type": "object", "properties": {
                    "content": {"type": "string", "description": "The fact to remember"},
                    "title": {"type": "string", "description": "Optional short title"}},
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
                    fp = _write_card(self.tenant_dir, content, args.get("title"))
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
