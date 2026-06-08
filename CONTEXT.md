# rtfm — Read The Full Manual

A local MCP server that makes large, can't-read-by-hand technical documentation — PDF spec
sets and documentation repositories — searchable, with grounded, cited answers. Published
as the `rtfm` plugin (skills `check` and `audit`). The "F" is for *Full*: it ingests the
whole manual so you can search all of it, not just skim.

## Language

**Corpus**:
The full set of documents the server makes searchable, across all configured Sources.
_Avoid_: index (means the FTS store, not the documents), library

**Source**:
One declared origin of documents the server indexes — read in place, never relocated or
copied out of where the files legitimately live. Typed by how it stays current (see below).
_Avoid_: collection, folder (too generic for the formal term)

**Manifest**:
The user- and agent-editable file that declares the list of Sources. Holds user-specific
paths, so it is never published — only a `*.example` template ships.
_Avoid_: config, registry, settings

### Source types

A Source is typed by its **sync method** (how it Refreshes), not by what files it holds.
File handling is decided per file by extension (PDF → page extraction; text/markup →
line-chunked).

**Dir source**:
A Source backed by a plain directory, indexed in place. Refresh is a no-op.
_Avoid_: folder source, local source

**Repo source**:
A Source backed by a git working tree. Two provenance flavors — **Linked** (user gives a
`path` to a tree they own) and **Managed** (user gives a `url`; the tool shallow-clones it
under the Corpus home). Both Refresh identically (fast-forward `git pull`, staleness-bounded).
_Avoid_: git source, vcs source

**Web source** _(planned — experimental)_:
A Source backed by one or more public URLs; Refresh re-fetches and caches the pages locally.
Best-effort and cooperative-only: it indexes whatever extractable text a page serves and
cannot read image-only or JavaScript-rendered sites (no OCR, no JS execution — the same
boundary that applies to image-only PDFs).
_Avoid_: url source, http source, remote source

### Operations

**Refresh**:
Advancing a Source to its upstream's latest content — one-way, never pushes. Implemented
per sync method (git fast-forward for Repo, re-fetch for Web, no-op for Dir). Staleness-
bounded: fires at most once per window, not once per query.
_Avoid_: pull (git-specific), sync (implies bidirectional), update

### Other

**Corpus home**:
The single directory holding all of the tool's state — the Manifest (`manifest.toml`), the
Default source's drop-dir (`default/`), and the FTS cache (`cache/`). Defaults to
`~/.rtfm/`, overridable via `RTFM_HOME`.
_Avoid_: config dir, data dir, root

**Default source**:
The always-present bootstrap Dir source — `mutable`, accepting any supported file type —
auto-created when no Manifest exists so the tool works with zero configuration. Named
`default`.
_Avoid_: misc, scratch, inbox

**Spec**:
A *kind of document* — a protocol or tool reference PDF (e.g. a large protocol specification
or a vendor product's manual set). The original and dominant PDF use case, but one flavor of
content, not the product.
_Avoid_: spec (as the name of the product or the server)
