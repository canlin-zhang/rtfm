---
status: accepted
---

# Doc-level signal (title/headings) ranked above body matches

Body-text BM25 carries no document-level signal: searching an exact document title can surface
incidental mentions scattered across other documents above the document that *bears* that title.
A `doc_fts` FTS5 table holds one row per unique content — `(sha256, title, headings)` — alongside
the per-chunk `content_fts`. `search` queries both: a document whose title or headings match ranks
**first**, then body matches (AND-first, OR/BM25 fallback). Every hit carries the document `title`;
a body hit reached via the OR fallback is flagged `fuzzy`; snippets pick the line covering the
most query terms. This is the Tier 1 search-quality win — richer extraction inside FTS5, not a
ranking rewrite (the engine rehaul is the reserved 1.0.0; see ROADMAP).

## Considered Options

- **Title/heading columns on `content_fts` (rejected).** `content_fts` is per-chunk (page/line);
  a per-document title duplicated onto every chunk row bloats the index and makes a title match
  resolve to an arbitrary chunk locator.
- **Separate `doc_fts` keyed by sha (chosen).** Doc-level signal lives once per content,
  content-addressed like `contents` (ADR 0010). A title/heading match resolves to the document
  and surfaces all its locations; `bm25` column weights rank title over headings.
- **Post-hoc Python re-rank of body hits (rejected).** Ad-hoc, ignores FTS `bm25`, and can't
  surface a bearing document whose body never repeats its own title.

## Extraction boundary (the scope guard)

Title = sane PDF metadata title, else the first substantial page-1 line; headings = the bookmark
outline (`doc.get_toc()`), which survives an image front-page — the failure mode where the visible
title isn't in the extracted text. Markup files use ATX/setext headings. A signal miss yields
empty fields and the document simply ranks on body text. Deliberately **not** done here: font-size
title detection, OCR for image front-pages, stemming tokenizer, `pdfgrep` raw fallback (ROADMAP).

## Consequences

- The bearing document wins title/heading queries; ranking stays explainable (doc-signal first,
  body follows) with weights as named constants, not a tuning project.
- Schema bumps to `user_version = 3`; the index is a cache, so the bump drops and rebuilds it and
  the next reindex repopulates `doc_fts` — no migration logic.
- Query robustness — AND→OR/BM25 fallback, short-token exclusion, a fuzzy-match marker, and
  best-line snippets — is retained, with doc-level signal layered on top of page-text matching.
