---
name: read-the-manual
description: Use when the user wants an answer sourced directly from the indexed document corpus, not from model knowledge. Triggers on "read the manual", "check the docs", "what does the doc/manual say about X", "find in the corpus", "answer from the docs".
---

# Read the Manual

Answer the user's question using **only** text found in the indexed corpus via rtfm. Do not answer from training knowledge. Every claim must be backed by a direct quote with a source, file, and format-native locator (page for PDFs, line for text).

## Invocation

The user invokes this skill directly, or the harness loads it when the description matches. The client-specific block appended to each front copy names the exact command and tool names.

## Routine

### 1. Search

Call the `search` tool with the key terms from the question. Note `sources_searched` to stay aware of the full corpus. Small new or edited files are auto-indexed on search; if the response carries a `STALE SOURCE` warning (a source has more new/changed files than the auto-reindex budget), build it yourself with the `reindex` tool, then search again.

If the first query returns nothing useful, try narrower or alternative terms — don't give up after one miss.

### 2. Read

For each promising hit, call the `read` tool with the source, relpath, and locator range to retrieve the actual text around the hit: read ±1–2 pages (PDF) or a surrounding line range (text) for full context. A hit's `locations` lists up to a few of the paths its content lives at (`total_locations` is the true count; `find_duplicates` gives the full list) — any one reads identically.

### 3. Answer

- Quote the corpus text **verbatim** for key claims, in block quotes.
- Cite every quote with source, file, and locator — `— *relpath* (source), p.N` for PDFs, `— *relpath* (source), line N` for text.
- If several documents cover the topic, synthesize but attribute each point separately.
- If the corpus has no clear answer, say so explicitly: "The indexed corpus doesn't cover this." Do NOT fill the gap with model knowledge.

### 4. Report missing references

After answering, scan the text you read for documents referenced by name that are not among the sources you searched. Report them compactly:

> **Referenced but not in corpus:** <Document Title>, <Document Title>

Include only documents cited directly by name, not vague phrases ("see the documentation"). If nothing is missing, skip this step silently.

## Rules

- **No knowledge fallback.** If it isn't in the corpus text you read this session, don't state it as fact.
- **No paraphrasing without citation.** If you summarize, immediately follow with the locator.
- **Prefer verbatim quotes** for precise technical claims (field widths, timing constraints, opcodes, state transitions, …).
- If the question is ambiguous, ask one clarifying question before searching.
