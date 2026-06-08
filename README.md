# rtfm

> **rtfm** — Read the ~~F-~~ **Full** Manual.

A local MCP server and Claude Code plugin that makes large, can't-read-by-hand technical
documentation searchable — PDF spec sets, documentation repositories (and, later, public
doc sites) — and answers questions strictly from that material, with grounded citations.

The eternal advice "read the manual" falls apart when the manual is a 400-page protocol
spec or a vendor doc-set no human can hold in their head. `rtfm` ingests the **full** manual
so the answer is one cited query away.

## Limitations

`rtfm` indexes the **extractable text** a document already carries. It does not OCR images
and does not execute JavaScript, so image-only/scanned PDFs and image-only or JS-rendered
web pages are out of scope — they index as empty. (See [ADR 0005](docs/adr/0005-text-extraction-only-no-ocr.md).)

## Install

```bash
# in Claude Code:
/plugin marketplace add canlin-zhang/rtfm
/plugin install rtfm
```

**Prerequisites:** `python3` on PATH is the only hard requirement. If `uv` is
installed it's used automatically (fast path); otherwise rtfm builds a private venv
under `~/.rtfm/venv` on first run — no manual dependency install either way.
*Optional:* install `poppler-utils` (`pdftotext`) for best-quality PDF extraction.

## Use

Drop PDFs / `.md` / `.txt` into `~/.rtfm/default/`, then ask Claude to search them, or
add more sources by editing `~/.rtfm/manifest.toml` (see `manifest.example.toml`).

## Status

Early but working. Today rtfm gives you zero-config search over a default drop-folder: PDF and
Markdown/text files are indexed into one full-text index with page/line citations, exposed
through the `search` / `read` / `list_sources` / `health_check` tools, with a uv-or-venv
launcher and Claude Code plugin packaging. HTML with anchor citations, git-backed repo sources
with auto-refresh, file-curation tools, and OCR are planned — see the [roadmap](docs/ROADMAP.md).

Architecture decisions live in [docs/adr/](docs/adr/); domain language in
[CONTEXT.md](CONTEXT.md).

## License

MIT © Canlin Zhang
