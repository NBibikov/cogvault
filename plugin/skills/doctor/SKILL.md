---
name: doctor
description: Check the health of the cogvault memory directory — cards without type, duplicate names, broken [[links]], and the recall log's no-hit rate (what the agent failed to find).
disable-model-invocation: true
---

Run these and summarise the findings for the user:

```bash
uvx cogvault stats   --tenant "${COGVAULT_TENANT:-$HOME/.cogvault/memory}"
uvx cogvault doctor  --tenant "${COGVAULT_TENANT:-$HOME/.cogvault/memory}"
uvx cogvault analyze
```

- `doctor` lists integrity problems. If there are mechanical ones (nested frontmatter,
  missing type, timestamp filenames), show the dry run of
  `uvx cogvault repair --tenant …` and ask before running it with `--apply`.
- `analyze` reports recalls, latency, and the **no-hit rate**. The recent no-hit
  queries are the memory gaps worth filling — offer to record cards for them.
