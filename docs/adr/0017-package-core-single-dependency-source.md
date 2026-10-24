---
status: accepted
---

# A package core behind a thin facade; pyproject is the single dependency source

The single-file rule was tied to the script distribution model: the PEP-723
`# /// script` block and `uv run script.py` made the file itself the artifact
(ADR 0009). The core is now published as the `rtfm-mcp` wheel, so the file is
no longer the artifact. What remains is a 2,223-line module whose labelled
sections are natural module boundaries, plus a dependency list declared twice
(the PEP-723 block and `pyproject.toml`) and kept in sync by a script.

## Decision

Split the core into an `rtfm/` package by responsibility:

- `config.py` — paths, extensions, schema/version constants, `Problem`-free
  configuration only.
- `manifest.py` — `Source` and manifest parsing/validation.
- `git_ops.py` — the git primitives; source-status helpers live with indexing.
  The name is not a bare `git`: it cannot be confused with the git CLI or a
  third-party `git` package in tracebacks.
- `extract.py` — PDF/text extraction, per-file rows, and doc-level signals.
- `staging.py` — the staged indexing pipeline (`StepResult`, `Problem`,
  `access`, `select`, `read_bytes_for`, `handle`, reporting types).
- `index.py` — the FTS store, schema, freshness, ingest, and `reindex_source`.
- `query.py` — plain search/read/duplicates/health functions.
- `tools.py` — the explicit `mcp.add_tool(...)` registration list.
- `app.py` — the single `mcp = FastMCP("rtfm")` object.

`rtfm_server.py` stays as a thin facade: it re-exports the six public tools,
`mcp`, and `main()`. The console entry point remains `rtfm_server:main`. Tests
use the owning modules; the facade is tested as a contract (ADR 0018).

`pyproject.toml` becomes the single dependency declaration. The PEP-723 block
is removed; `bin/launch` installs the project (`pip install -e .`) into its
venv fallback; `scripts/check_version_consistency.py` no longer mirrors a
second dependency list. `uv run rtfm_server.py` still works because uv reads
the project manifest.

The wheel contains the `rtfm` package plus the facade. The front bootstraps and
the published `rtfm-mcp` install path are unchanged.

## Considered Options

- **Keep the single file.** Rejected: its distribution rationale is gone, and
  the labelled sections already describe the split. The file's size is now a
  navigation and review cost with no artifact benefit.
- **Keep PEP-723 and pyproject as two declarations.** Rejected: the mirror is
  maintenance without a remaining consumer once the launcher installs the
  project.
- **Split without the facade.** Rejected: it removes the console compatibility
  surface for no gain. The facade is now small and tested as a contract, not as
  the test API.

## Consequences

- This is a behavior-preserving refactor; the existing suite is the gate. The
  test suite moves to module-owned, behavior-named files (ADR 0018), and the
  eight monkeypatch sites retarget to public seams.
- Tools are plain functions in `query.py`; `tools.py` registers them with
  `mcp.add_tool`, so the tool list is explicit and testable without decorators.
- `bin/launch`'s venv path installs the project instead of parsing PEP-723, so
  the launcher and the published package share one dependency source.
- The refactor ships as its own version after the front release; the fronts
  keep pinning the last published core until then.
