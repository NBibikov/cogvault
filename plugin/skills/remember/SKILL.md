---
name: remember
description: Save a fact to cogvault memory. With arguments, store that text as one card; without, distil the durable facts from this session and store each as its own card.
argument-hint: "[fact to remember]"
---

Save to cogvault memory with the `cogvault_record` tool.

**With arguments** (`$ARGUMENTS` is not empty): store that text as a single card.
Pick a short `title` and the right `type` (`feedback` for a rule or correction,
`project` for work or a decision, `reference` for a pointer, `user` for who the user
is). Keep the user's wording; add a **Why:** line only if they gave a reason.

**Without arguments:** review this session and pick out what should survive it —
decisions with their reasons, non-obvious fixes, corrections, preferences, where
things live. Before each record, `cogvault_recall` the topic: if a card already
covers it, say so instead of writing a duplicate. Record one fact per card.

Never store secrets, tokens, or passwords — only where they are kept.

Finish with a short list of the cards you wrote (title and type).
