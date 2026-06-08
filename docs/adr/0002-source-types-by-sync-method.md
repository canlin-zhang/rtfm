---
status: accepted
---

# Source types split on sync method, not content; index by file extension

A Source is typed by how it stays current, not by what it holds: `dir` (a plain directory,
indexed in place, no VCS) or `repo` (a git working tree, pull-able to its latest commit
before indexing). File handling is chosen per file by extension — PDF gets page extraction
and outlines, text/markup gets line-chunked indexing — so a single Source may hold both.
This replaces the inherited content-based split (`spec` PDFs vs `repo` text trees), which
conflated "what kind of file" with "how to sync" and left git-backed PDF sets and
mixed-content repos unrepresentable.

## Considered Options

- **Content-based split (inherited).** `spec` (PDF; own FTS table + tool family) vs `repo`
  (text; own FTS table + tool family). Rejected: a folder of PDFs that is also a git
  checkout, or a doc repo containing a few PDFs, cannot be expressed; and "pull latest"
  ends up stapled to the text-tree type rather than to git-ness.
- **Sync-based split (chosen).** `dir` vs `repo`, with extension deciding file handling.
  Unifies the two parallel FTS schemas and tool families into one indexing layer and
  attaches "pull latest" to exactly the git-backed Sources.

## Consequences

- One set of corpus tools (`search` / `read` / `list`) spans all Sources; PDF-only
  features (outline, duplicate detection) key off file extension, not Source type.
- A bigger one-time rewrite of the indexing layer than keeping the inherited split —
  accepted because this is a from-scratch public extraction, so the seam is cheapest to
  remove now.
- "Pull latest" is well-defined only on `repo` Sources.
