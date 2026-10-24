# AGENTS.md

Guidance for AI agents (and humans) working on **rtfm**. Read this before making changes.
rtfm is one core plus a thin Claude Code and OpenCode front, and is maintained with AI
agents, so this file is the operational entry point; the *why* behind every rule lives in
[`docs/adr/`](docs/adr/) and the domain vocabulary in [`CONTEXT.md`](CONTEXT.md).

## Quickstart

```bash
uv run pytest -q          # run the test suite (uv bootstraps deps from pyproject.toml)
uvx ruff check .          # lint (config in pyproject.toml [tool.ruff]); add --fix to autofix
uv run rtfm_server.py     # run the MCP server directly (dev; installed fronts use bootstrap.py)
```

`python3` on PATH with a working `venv`/`pip` module is the only hard prerequisite; `uv` is the
fast path; `poppler` (`pdftotext`) is an optional PDF-quality bonus (the `pymupdf`/`pypdf`
fallback covers extraction without it). Supported platforms: Linux and macOS; Windows is not
officially supported.

## Layout

- `rtfm/` — the core package: `config`, `manifest`, `git_ops`, `extract`, `staging`,
  `index`, `query`, `tools`, `app` (ADR 0017).
- `rtfm_server.py` — the thin console facade: six public tools, `mcp`, and `main()`.
- `bin/launch` — source/development launcher: prefers `uv run`, else a self-managed venv.
- `manifest.example.toml` — documented source-manifest template.
- `plugins/claude/` — Claude Code front: manifest, `.mcp.json`, bootstrap, skill copy.
- `plugins/opencode/` — OpenCode front: `package.json`, `index.ts`, bootstrap, skill copy.
- `skills/read-the-manual/` — shared skill prefix; the front copies append only their client block.
- `CLAUDE.md` — imports `AGENTS.md` and adds Claude-only development notes.
- `opencode.json` — points at `docs/opencode-dev.md`, the OpenCode-only notes.
- `.claude-plugin/marketplace.json` — the Claude marketplace catalog (the only root plugin file).
- `docs/adr/` — architecture decisions (numbered). `docs/ROADMAP.md` — deferred scope.
- `tests/` — pytest, mirroring the modules (`test_<module>.py`) plus named behavior
  exceptions (`test_repo_sources.py`, `test_search_quality.py`, `test_e2e.py`) and
  `test_facade.py` (ADR 0018).

## Hard structural rules

1. **The core is the `rtfm/` package; `rtfm_server.py` is a facade** (ADR 0017).
   Dependencies come from `pyproject.toml`; there is no PEP-723 block. Cross-module calls
   go through the module object (`git_ops.fetch(...)`, `staging.access(...)`) so a fake or
   monkeypatch at a public seam affects every caller. Public, test-facing functions drop the
   leading underscore; true internals keep it.
2. **MCP tools are plain functions in `rtfm/query.py`; `rtfm/tools.py` registers them**
   with `mcp.add_tool`, and `rtfm/app.py` owns the single `mcp = FastMCP("rtfm")` object.
   Tests call the plain functions directly. `rtfm_server.py` imports `rtfm.tools` for the
   registration side effect, re-exports the six names, and keeps `main()` plus the
   `if __name__ == "__main__": main()` guard.
3. **A front never contains `rtfm_server.py`.** `plugins/claude/` and `plugins/opencode/`
   are thin: each installs `rtfm-mcp` at the version declared by its own manifest. The core
   is published once; the fronts only register it and deliver the skill.

## Design invariants — do not break (rationale in the ADRs)

- **Read-only by default.** Indexing/search never mutate files. Mutation tools and the
  audit/reorg skills run only on a Source explicitly marked `mutable: true` in the manifest,
  and **an agent must never set `mutable` itself** — that flag is the user's recorded consent.
  ([ADR 0001](docs/adr/0001-read-only-by-default.md))
- **Source types split on sync method** (`dir` / `repo` / future `web`); file handling is
  keyed by extension into one FTS index. ([ADR 0002](docs/adr/0002-source-types-by-sync-method.md))
- **Refresh** is staleness-bounded `git pull --ff-only`, fail-soft with cause-distinguishing
  messages. ([ADR 0003](docs/adr/0003-refresh-model.md))
- **Format-native locators**: PDF→page, text→line, HTML→anchor.
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
