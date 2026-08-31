# AGENTS.md

Guidance for AI agents (and humans) working on **rtfm**. Read this before making changes.
rtfm is itself a Claude Code plugin and is maintained with AI agents, so this file is the
operational entry point; the *why* behind every rule lives in [`docs/adr/`](docs/adr/) and
the domain vocabulary in [`CONTEXT.md`](CONTEXT.md).

## Quickstart

```bash
uv run pytest -q          # run the test suite (uv bootstraps deps from pyproject.toml)
uvx ruff check .          # lint (config in pyproject.toml [tool.ruff]); add --fix to autofix
uv run rtfm_server.py     # run the MCP server directly (normally launched via bin/launch)
```

`python3` is the only hard prerequisite; `uv` is the fast path; `poppler` (`pdftotext`) is an
optional PDF-quality bonus (the `pymupdf`/`pypdf` fallback covers extraction without it).

## Layout

- `rtfm_server.py` — the entire server (FastMCP). **One file on purpose** (see below).
- `bin/launch` — launcher: prefers `uv run`, else a self-managed venv.
- `manifest.example.toml` — documented source-manifest template.
- `.claude-plugin/`, `.mcp.json` — plugin + marketplace manifests.
- `docs/adr/` — architecture decisions (numbered). `docs/ROADMAP.md` — deferred scope.
- `tests/` — pytest, one file per concern.

## Hard structural rules

1. **`rtfm_server.py` stays a single file.** Its PEP-723 `# /// script` block + the
   `uv run script.py` distribution model are per-file. Do **not** split it into a package.
   It is organized into labelled sections (`config` / `index` / `manifest` / `extractors` /
   `tools`); keep logic in importable module-level functions.
2. **MCP tools are thin `@mcp.tool()` wrappers** over those plain functions, so tests call
   the helpers directly. The `mcp = FastMCP("rtfm")` line must precede the tool defs; the
   `if __name__ == "__main__": mcp.run()` block stays last.

## Design invariants — do not break (rationale in the ADRs)

- **Read-only by default.** Indexing/search never mutate files. Mutation tools and the
  audit/reorg skills run only on a Source explicitly marked `mutable: true` in the manifest,
  and **an agent must never set `mutable` itself** — that flag is the user's recorded consent.
  ([ADR 0001](docs/adr/0001-read-only-by-default.md))
- **Source types split on sync method** (`dir` / `repo` / `web`, the last flavored by
  hosting family); file handling is keyed by extension into one FTS index.
  ([ADR 0002](docs/adr/0002-source-types-by-sync-method.md))
- **Refresh** is staleness-bounded `git pull --ff-only` for repos; web sources refresh by
  explicit `reindex` re-fetch only (search never fetches for web), fail-soft with
  cause-distinguishing messages. ([ADR 0003](docs/adr/0003-refresh-model.md),
  [ADR 0014](docs/adr/0014-web-source-type.md))
- **Format-native locators**: PDF→page, text/HTML→line (web: page URL derivable from the
  source `url` plus `relpath`).
  ([ADR 0004](docs/adr/0004-format-native-locators.md))
- **Text-extraction only** — no OCR, no JS. ([ADR 0005](docs/adr/0005-text-extraction-only-no-ocr.md))
- **The defining guardrail:** no vendor-, project-, or domain-specific tables in shipped
  code or skills. Grouping/heuristics stay domain-neutral; specialized knowledge belongs in
  the *user's* private config, never the published artifact.
  ([ADR 0008](docs/adr/0008-skills-corpus-general-no-vendor-tables.md))

**Before changing behavior, read the relevant ADR. For any consequential or hard-to-reverse
decision, add a new numbered ADR** (`docs/adr/NNNN-slug.md`) following the existing format.

## Public-artifact hygiene

This is a **public** repository — everything committed (code, comments, docstrings, docs,
config, **and commit messages**) is published. Keep it domain-neutral and free of internal
context:

- **No internal planning vocabulary** in shipped files — no phase or "Plan N" labels, sub-PR
  codenames, milestone names, or task IDs. Implementation plans live in the gitignored
  `docs/superpowers/` and never ship. Describe a feature by what it does, not by which plan
  delivers it ("added later", not "the next plan adds this").
- **No vendor, employer, project, or customer names.** Use generic examples (`vendor-tool`,
  `acme-docs`); no domain-specific tables (see
  [ADR 0008](docs/adr/0008-skills-corpus-general-no-vendor-tables.md)).
- **No private or personal data** — no `/home/<user>` paths, hostnames, IPs, secrets/tokens,
  or corporate email addresses. The maintainer contact is a dedicated public address.
- **Nothing extracted or copyrighted** — index databases and indexed source documents are
  gitignored; only code and docs ship.

Before committing, read your diff and sweep for leaks, e.g.:

```bash
git grep -inE 'plan [0-9]|/home/[a-z]|[a-z0-9._%+-]+@[a-z0-9.-]+|secret|token|api[_-]?key'
```

## Code style

- `snake_case` throughout; `PascalCase` classes; type hints on public signatures.
- Comment the **why**, not the what — one or two tight lines, citing external constraints.
- Errors are **loud and actionable**: `!!! ERROR !!! <what> … Recover: <how>`. Query/search
  helpers must **never raise** on arbitrary user input — degrade and return.
- Enforced mechanically by ruff (`E`, `F`, `I`, `UP`, `B`; line length 100).
- Tests target boundary cases that catch real bugs, not coverage padding.

## Contributing

`main` is protected — all changes land via pull request, and CI (tests + ruff) must pass.
See [CONTRIBUTING.md](CONTRIBUTING.md).
