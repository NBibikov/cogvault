# cogvault plugin for Claude Code

Persistent memory for Claude Code over plain Markdown cards you own — hybrid recall
(vector + BM25), offline, no LLM in the loop.

```text
/plugin install cogvault --marketplace NBibikov/cogvault
```

- **MCP server** `cogvault`, started with `uvx cogvault mcp` (needs [uv](https://docs.astral.sh/uv/)).
  Memory lives in `~/.cogvault/memory`; set `COGVAULT_TENANT` to use another directory,
  for example Claude Code's own auto-memory under `~/.claude/projects/<project>/memory`.
- **`memory` skill** — tells the agent when to recall and what is worth recording.
- **`/cogvault:remember [fact]`** — save one fact, or distil the session into cards.

Health check from your shell (not part of the plugin): `uvx cogvault doctor --tenant ~/.cogvault/memory`,
and `uvx cogvault analyze` for the recall log's no-hit queries.

Cards are ordinary Markdown files: open them, edit them, `git diff` them. The index is a
rebuildable cache. Full docs: <https://github.com/NBibikov/cogvault>.
