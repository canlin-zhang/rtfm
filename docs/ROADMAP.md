# Roadmap

Deferred scope, each with the decision that parked it. Not yet implemented.

- **Web sources** — index public doc sites as a third sync type alongside `dir` and `repo`
  (`web`, Refreshed by re-fetch). Experimental and cooperative-only: no OCR, no JS
  execution. See [ADR 0002](adr/0002-source-types-by-sync-method.md) (reserves the type) and
  [ADR 0005](adr/0005-text-extraction-only-no-ocr.md) (the boundary).

- **OCR** — extract text from image-only/scanned documents. **PDF-first** (scanned specs are
  the real demand), HTML/web image content only after. See
  [ADR 0005](adr/0005-text-extraction-only-no-ocr.md).

- **Publish to PyPI** — an additional install channel (`pipx install rtfm`) alongside the
  plugin + `uv`/venv launcher, for users who prefer a standard Python package. See
  [ADR 0009](adr/0009-launcher-uv-preferred-venv-fallback.md).

- **Prune orphaned managed clones** — a `prune` tool / health-check report for managed-repo
  clones left under the Corpus home after their Source is removed from the Manifest. See
  [ADR 0007](adr/0007-repo-linked-vs-managed.md).

- **Search quality via richer extraction** — keyword BM25 over page/line text carries no
  document-level signal: searching even an exact document title can surface incidental mentions
  scattered across other documents rather than the doc that bears that title. Tackle it by
  extracting *more from the corpus*, not by reinventing ranking: capture document **title**
  (PDF metadata + front-page heading), **outline/TOC** (`fitz.get_toc()`), and headings; index
  them as high-weight fields and surface the title in hits. First determine whether the titled
  doc's text is even extracted (a front-page title may be an image) vs. merely out-ranked.
  Deferred from Plan 1's page-text-only MVP; **sequenced after the skill work**, which rides on
  search relevance.

- **Mutation tools + the `audit` skill (Plan 4)** — `rename`/`delete` gated to `mutable`
  Sources (ADR 0001), plus the de-vendored `audit` skill (sha256 dedup via `find_duplicates`
  + title-page version extraction + collision-checked, approval-gated rename proposals; ADR
  0008). `audit` is designed but deferred until the mutation tools land — without them it can
  *propose* normalized names but cannot *execute* the renames.
