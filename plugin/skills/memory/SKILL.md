---
name: memory
description: Use the cogvault memory tools. Recall before answering anything that may depend on earlier sessions (past decisions, bug fixes, infra details, user preferences, "what did we decide", "how do we deploy"); record durable facts after a decision, a non-obvious fix, or a correction from the user.
---

# Working with cogvault memory

The `cogvault` MCP server gives you two tools over a directory of Markdown cards
(default `~/.cogvault/memory`, override with `COGVAULT_TENANT`).

## Recall first

Call `cogvault_recall` **before** answering when the answer may live in earlier work:
a past decision, a fix that was found once, how something is deployed, a preference
the user stated, a credential's *location* (never the secret itself).

- Phrase the query the way the card would be titled: `deploy rules for the worker`,
  not `hey what was that thing`.
- Filter with `type` when you know the kind: `feedback` (rules, corrections),
  `project` (ongoing work, decisions), `reference` (pointers), `user` (who the user is).
- The top hit may carry a `Related:` line — follow it with a second recall if the
  first card points elsewhere.
- No hit is information too: say the memory has nothing on it instead of guessing.

## Record what should survive the session

Call `cogvault_record` after something worth knowing next week:

- a decision and **why** it was made;
- a non-obvious bug fix or workaround;
- a correction or standing preference from the user;
- where a resource lives (URL, path, dashboard).

One fact per card. Give it a short `title` (it becomes the filename) and a `type`.
Lead with the fact, then a **Why:** line. Do not record secrets, raw logs, or
anything the repository or git history already holds.

## Don't

- Don't recall on every turn — only when earlier context could change the answer.
- Don't record session chatter or step-by-step narration.
- Don't edit `.cogvault.db`; it is a derived cache. The Markdown cards are the truth
  and can be edited directly.
