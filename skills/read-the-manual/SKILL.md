---
name: read-the-manual
description: Use when the user wants an answer sourced directly from the indexed document corpus, not from model knowledge. Triggers on "/read-the-manual", "read the manual", "check the docs", "what does the doc/manual say about X", "find in the corpus", "answer from the docs".
---

# Read the Manual

Answer the user's question using **only** text found in the indexed corpus via rtfm. Do not answer from training knowledge. Every claim must be backed by a direct quote with a source, file, and format-native locator (page for PDFs, line for text).

## Trigger

- User invokes `/read-the-manual`.
- User says "read the manual", "check the docs", "what does the manual say about X", "find in the corpus", or similar phrasing signalling they want corpus-sourced truth rather than model knowledge.

## Routine

### 1. Search

Call `mcp__plugin_rtfm_rtfm__search(query)` with the key terms from the question. Note `sources_searched` to stay aware of the full corpus. Small new or edited files are auto-indexed on search; if the response carries a `STALE SOURCE` warning (a source has more new/changed files than the auto-reindex budget), build it yourself with `mcp__plugin_rtfm_rtfm__reindex('<source>')`, then search again.

If the response carries a `sources_failed` list, each named source has NO indexed content (never indexed, or the last index attempt failed — the state string says which). It is not part of the corpus: do not claim coverage for it. You may attempt `mcp__plugin_rtfm_rtfm__reindex('<name>')` once; if it fails again, state the gap to the user.

If the first query returns nothing useful, try narrower or alternative terms — don't give up after one miss.

### 2. Read

For each promising hit, call `mcp__plugin_rtfm_rtfm__read(source, relpath, start, end)` to retrieve the actual text around the hit's locator: read ±1–2 pages (PDF) or a surrounding line range (text) for full context. A hit's `locations` lists up to a few of the paths its content lives at (`total_locations` is the true count; `find_duplicates` gives the full list) — any one reads identically.

For a web source, `relpath` is the page path under the version root; the page's public URL is the source's index `url` up to its last `/`, plus `relpath` (`list_sources` shows the index url).

### 3. Answer

- Quote the corpus text **verbatim** for key claims, in block quotes.
- Cite every quote with source, file, and locator — `— *relpath* (source), p.N` for PDFs, `— *relpath* (source), line N` for text.
- For web sources, cite the page's public URL instead of a file: `— <public url>, line N`.
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
