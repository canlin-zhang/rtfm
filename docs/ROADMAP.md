# Roadmap

Deferred scope, each with the decision that parked it. Not yet implemented.

- **Web sources — ✅ SHIPPED in 0.7.0 (ADR 0014).** The `web` source type with the
  `readthedocs` flavor: indexes ReadTheDocs-hosted doc sites whose projects build no PDF.
  Cooperative-only: no OCR, no JS execution. See
  [ADR 0002](adr/0002-source-types-by-sync-method.md) (reserves the type),
  [ADR 0005](adr/0005-text-extraction-only-no-ocr.md) (the boundary), and
  [ADR 0014](adr/0014-web-source-type.md).

  - **Browser escalation (planned, opt-in)** — per-source opt-in "try harder" mode for web
    Sources whose cooperative fetch fails (bot wall, rate limit, client-rendered shell page):
    the user asks the agent to index the site anyway, the agent flips a manifest flag, and the
    failed source re-fetches through a headless browser. The browser is never the default fetch
    path — escalation fires only after the cooperative fetch failed, and the browser is lazily
    installed on first escalated run (no cold-start cost for everyone else). Extraction stays
    unchanged (the rendered page still yields `role="main"`). Deferred: an arms-race dependency
    (bot-detection bypasses rot), a heavy per-install browser binary, and a fetch-transport
    that belongs to the web type generally, not to the readthedocs flavor. Wheel not reinvented:
    Playwright is the designated implementation, when this lands.

- **OCR** — extract text from image-only/scanned documents. **PDF-first** (scanned specs are
  the real demand), HTML/web image content only after. See
  [ADR 0005](adr/0005-text-extraction-only-no-ocr.md).

- **Publish to PyPI** — an additional install channel (`pipx install rtfm`) alongside the
  plugin + `uv`/venv launcher, for users who prefer a standard Python package. See
  [ADR 0009](adr/0009-launcher-uv-preferred-venv-fallback.md).

- **Prune orphaned managed clones** — a `prune` tool / health-check report for managed-repo
  clones left under the Corpus home after their Source is removed from the Manifest. See
  [ADR 0007](adr/0007-repo-linked-vs-managed.md).

- **Search quality** — keyword BM25 over page/line text carries no document-level signal:
  searching even an exact document title can surface incidental mentions scattered across other
  documents rather than the doc that bears that title. Two tiers, by cost and by version:

  - **Tier 1 — richer extraction within FTS5. ✅ SHIPPED in 0.5.0 (ADR 0012).** Document
    **title** (PDF metadata / first page-1 line) and **headings/outline** (`doc.get_toc()`,
    markup headings) are indexed in a `doc_fts` table and ranked above body matches; the title is
    surfaced in every hit. Stayed inside SQLite FTS5 — extract more, don't reinvent ranking.

  - **Tier 2 — search-engine rehaul (the 1.0.0 release).** A complete overhaul of the search and
    index mechanism, replacing the hand-rolled FTS5 index + ranking with an existing open-source
    full-text search framework (candidate: Whoosh / Whoosh-Reloaded; specific framework TBD).
    This is the reserved **1.0.0** milestone — the first release whose relevance no longer rests
    on a bespoke index. Evaluate before committing: dependency weight vs. the FastMCP/`uv`
    launcher (ADR 0009), native field-boosting and phrase/proximity support, and a migration
    path off the content-addressed FTS5 schema.

  - **Considered and deferred (FTS5 wins not taken in Tier 1).** Two classic FTS techniques,
    parked with reasons rather than shipped blind:
    - **`pdfgrep` / raw-PDF fallback** — grep the raw PDF when the FTS index finds nothing, as a
      recall safety net. Deferred: it adds an external tool dependency and is a *different
      mechanism* from FTS5; rtfm's content-addressed extraction is the source of truth. Revisit
      only if extraction gaps prove to lose real hits.
    - **Porter / stemming tokenizer** — `tokenize='porter'` would improve recall (e.g. `index`
      matches `indexing`); rtfm doesn't use it. Deferred: genuine *precision* risk on a technical
      corpus (opcodes, field names, `flit`/`flits`, version-like tokens) where over-stemming
      surfaces noise. Evaluate against the real corpus as its own change, not bundled into the
      Tier 1 doc-signal work.

- **Mutation tools + the `audit` skill (Plan 4)** — `rename`/`delete` gated to `mutable`
  Sources (ADR 0001), plus the de-vendored `audit` skill (sha256 dedup via `find_duplicates`
  + title-page version extraction + collision-checked, approval-gated rename proposals; ADR
  0008). `audit` is designed but deferred until the mutation tools land — without them it can
  *propose* normalized names but cannot *execute* the renames.
