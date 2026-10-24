# rtfm

> **rtfm** — Read the ~~F-~~ **Full** Manual.

A local MCP server and Claude Code plugin that makes large, can't-read-by-hand technical
documentation searchable — PDF spec sets, documentation repositories (and, later, public
doc sites) — and answers questions strictly from that material, with grounded citations.
The server is also published as the `rtfm-mcp` Python package.

The eternal advice "read the manual" falls apart when the manual is a 400-page protocol
spec or a vendor doc-set no human can hold in their head. `rtfm` ingests the **full** manual
so the answer is one cited query away.

## Limitations

`rtfm` indexes the **extractable text** a document already carries. It does not OCR images
and does not execute JavaScript, so image-only/scanned PDFs and image-only or JS-rendered
web pages are out of scope — they index as empty. (See [ADR 0005](https://github.com/canlin-zhang/rtfm/blob/main/docs/adr/0005-text-extraction-only-no-ocr.md).)

## Install

### Claude Code

```bash
# in Claude Code:
/plugin marketplace add canlin-zhang/rtfm
/plugin install rtfm
```

### Any MCP client (PyPI)

The server is also published as `rtfm-mcp`:

```bash
uvx --from rtfm-mcp rtfm-server      # or: pipx install rtfm-mcp && rtfm-server
```

Point the client's MCP configuration at that command:

```json
{
  "mcpServers": {
    "rtfm": {
      "command": "uvx",
      "args": ["--from", "rtfm-mcp", "rtfm-server"]
    }
  }
}
```

**Prerequisites:** `python3` on PATH is the only hard requirement for the plugin. It
uses `uv` when available and otherwise builds a private venv under `~/.rtfm/venv` on
first run. The PyPI path needs `uv` (for `uvx`) or `pipx`.
*Optional:* install `poppler-utils` (`pdftotext`) for best-quality PDF extraction.

## Two ways to use it

**Quick-and-dirty (the `default` drop-dir).** Drop PDFs or Markdown/text into `~/.rtfm/default`
and just `search` — the default dir re-indexes itself on search, so dropped files show up with
no extra step. Keep it small; it's a scratch space.

**Organized corpus (pointed-to folders).** For a large, structured doc set (e.g. a vendor's
documentation tree), add a `dir` source in `~/.rtfm/manifest.toml` pointing at the folder, then
build it once:

```
reindex("vendor-docs")
```

Searches are instant afterward. `dir` sources auto-reindex inline when the delta is small
(`RTFM_AUTO_REINDEX_MAX`, default 10); a larger delta returns a warning telling you to run
`reindex` once. `git_repo` sources are the exception to the budget: a stale or never-indexed
source always auto-reindexes inline on search — no budget gate, warns loudly on failure —
including the initial clone for managed sources, so the first search over a fresh corpus can
take a while. Byte-identical files that repeat across version subfolders are extracted once;
`find_duplicates` lists every path a given content lives at.

## Use

Drop PDFs / `.md` / `.mdx` / `.rst` / `.txt` into `~/.rtfm/default/`, then ask Claude to search them, or
add more sources by editing `~/.rtfm/manifest.toml` (see `manifest.example.toml`).

Or invoke the bundled **`read-the-manual`** skill (`/read-the-manual`) to have a question
answered strictly from the corpus — verbatim quotes with page/line citations, no
model-knowledge fallback.

## Status

Early but working. Today rtfm gives you zero-config search over a default drop-folder: PDF,
Markdown, reStructuredText, and text files are indexed into one content-addressed full-text
index with page/line citations, exposed through the `search` / `read` / `reindex` /
`find_duplicates` / `list_sources` / `health_check` tools and the `read-the-manual` skill, with
a uv-or-venv launcher and Claude Code plugin packaging. HTML with anchor citations, git-backed
repo sources with auto-refresh, file-curation tools, and OCR are planned — see the
[roadmap](https://github.com/canlin-zhang/rtfm/blob/main/docs/ROADMAP.md).

Architecture decisions live in [docs/adr/](https://github.com/canlin-zhang/rtfm/tree/main/docs/adr/); domain language in
[CONTEXT.md](https://github.com/canlin-zhang/rtfm/blob/main/CONTEXT.md).

## License

MIT © Canlin Zhang
