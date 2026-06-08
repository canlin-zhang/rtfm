---
status: accepted
---

# Content-addressed extraction store

Extraction is keyed by content hash (sha256), not by path. A `contents` table holds one row
per unique content (locator kind, chunk count, extraction status); a `content_fts` FTS5 table
holds the searchable chunks keyed by sha; a `locations` table maps each `(source, relpath)` to
its sha. This supersedes the implicit `(source, relpath)`-keyed FTS, under which a byte-identical
file appearing under N version paths was extracted and stored N times.

## Considered Options

- **Path-keyed FTS (inherited).** One FTS row-set per `(source, relpath)`. Rejected: vendor doc
  sets ship the same PDFs byte-identical across many version paths; extraction cost and index
  size scaled with paths, not unique content.
- **Content-addressed store (chosen).** Extract once per unique sha; map all paths to it.

## Consequences

- Search hits are dedup'd to one per unique content and disclose every path the content lives
  at (capped per hit; `find_duplicates` gives the full list).
- The index DB carries `PRAGMA user_version`; a mismatch drops and rebuilds the cache.
- Extraction parallelizes over unique contents, so dedup cuts the worker job count first.
