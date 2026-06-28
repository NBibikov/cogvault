"""
cogvault.mcp_server — stdio MCP server exposing recall/record over one tenant vault.

Run:  cogvault mcp --tenant ~/agent/memory
Tools: cogvault_recall (hybrid search), cogvault_record (append a markdown card).
"""
from __future__ import annotations
import sys, json, os, datetime
from .core import Vault, Config

PROTOCOL = "2024-11-05"


def _write_card(tenant_dir: str, content: str, title: str | None = None) -> str:
    """Append a memory as a real markdown file (source of truth)."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = (title or content[:40]).lower()
    slug = "".join(c if c.isalnum() else "-" for c in slug).strip("-")[:50] or "card"
    fp = os.path.join(tenant_dir, f"card-{ts}-{slug}.md")
    body = f"# {title}\n\n{content}\n" if title else content + "\n"
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
                    "limit": {"type": "integer", "default": 5}},
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
                                  "serverInfo": {"name": "cogvault", "version": "0.5.0"}})
        if m == "notifications/initialized":
            return None
        if m == "tools/list":
            return self._ok(rid, {"tools": [
                {"name": n, **spec} for n, spec in self.tools.items()]})
        if m == "tools/call":
            p = req.get("params", {})
            name = p.get("name"); args = p.get("arguments", {})
            try:
                if name == "cogvault_recall":
                    res = self.vault.search(args["query"], k=args.get("limit", 5))
                    if not res:
                        text = "No matching memories found."
                    else:
                        text = "\n\n".join(
                            f"[{r['score']}] {r['file']}\n{r['text']}" for r in res)
                    return self._ok(rid, {"content": [{"type": "text", "text": text}]})
                if name == "cogvault_record":
                    fp = _write_card(self.tenant_dir, args["content"], args.get("title"))
                    self.vault.reindex()
                    return self._ok(rid, {"content": [{"type": "text",
                            "text": json.dumps({"status": "ok", "file": os.path.basename(fp)})}]})
                return self._err(rid, -32601, f"unknown tool {name}")
            except Exception as e:
                return self._err(rid, -32000, str(e))
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
