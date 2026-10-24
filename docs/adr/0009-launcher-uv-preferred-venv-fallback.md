---
status: accepted
---

# Server launch: uv-preferred with a venv fallback; python3 is the only hard prereq

In source and development runs, `bin/launch` is the launcher rather than `uv` directly,
because a static command can't branch on what's installed. The launcher:

- uses `uv run` when `uv` is on PATH — the fast path, unchanged for environments that ship
  uv (e.g. CI images or managed dev environments that preinstall it);
- otherwise creates/reuses a venv under `~/.rtfm/venv`, installs the project editable
  (`pip install -e .`), and runs `python -m rtfm_server` (cached; reinstalls when the
  pyproject dependency set changes);
- reads the dependency list from `pyproject.toml`, the single source of truth for deps
  (ADR 0017);
- fails loudly and actionably if neither uv nor python3+pip is usable — never a silent hang.

_Distribution note (ADR 0016):_ fronts no longer carry the server file. A thin
bootstrap prefers `uvx` / `uv tool run`; otherwise it installs the pinned
`rtfm-mcp` package into a harness-private venv and runs `python -m rtfm_server`.
The launcher described here remains for source and development runs.

The only hard prerequisite is therefore **`python3` on PATH** with a working
`venv`/`pip` module (Debian/Ubuntu: `python3-venv`). `uv` is a speed bonus;
`poppler` is a PDF-quality bonus (the pymupdf/pypdf fallback chain covers
extraction without it).

## Considered Options

- **uv-only** — simplest, no launcher to maintain, but a non-uv user must `pip install uv`
  first. Rejected as the *default* for a public audience; uv remains the preferred fast path.
- **Publish to PyPI, install via pipx** — the standard Python distribution path. Adopted
  for the core (`rtfm-mcp`): the fronts install it through `uvx`/`uv` or a private venv;
  the launcher remains for source runs.

## Consequences

- First run on the fallback path is slower (one-time venv + pip install), cached thereafter.
- The dev launcher's venv lives under the Corpus home (`~/.rtfm/venv`); a front's fallback
  venv lives under its own data dir (`~/.rtfm/fronts/<front>/venv`, or
  `$CLAUDE_PLUGIN_DATA/venv` for Claude), so deleting the Corpus home resets only the dev one.
- The core is published as `rtfm-mcp`; the fronts pin its version.
