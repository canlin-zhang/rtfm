# Roadmap

Deferred scope, each with the decision that parked it. Not in the first release.

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
