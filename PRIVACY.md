# Privacy

cogvault runs entirely on your machine. It has no account, no server, and no telemetry
sent anywhere.

**What it stores, all locally:**

- your memory cards — the Markdown files in the tenant directory you point it at;
- a derived search index under `~/.cache/cogvault/` (one SQLite file per tenant), rebuildable from the cards;
- a recall log (`~/.cache/cogvault/query-log.jsonl`) with each query's text, the number of
  results, the top card's filename, scores and latency, used by `cogvault analyze`. Turn it off with `COGVAULT_LOG=off`, or move it
  with `COGVAULT_LOG=/path.jsonl`.

**What touches the network:**

- installing or launching through `uvx` / `pip` downloads the package from PyPI;
- the first run downloads the embedding model (ONNX weights) from Hugging Face into
  `~/.cache/fastembed`. After that, indexing and recall work offline.

cogvault itself makes no other network requests. Your cards, queries and results are never
uploaded — there is nowhere for them to go.

Questions: <https://github.com/NBibikov/cogvault/issues>
