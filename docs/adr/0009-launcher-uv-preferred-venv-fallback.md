---
status: accepted
---

# Server launch: uv-preferred with a venv fallback; python3 is the only hard prereq

The plugin's `.mcp.json` runs a small launcher (`bin/launch`) rather than `uv` directly,
because a static MCP command can't branch on what's installed. The launcher:

- uses `uv run` when `uv` is on PATH — the fast path, unchanged for environments that ship
  uv (e.g. CI images or managed dev environments that preinstall it);
- otherwise creates/reuses a venv under `~/.rtfm/venv`, pip-installs the deps, and runs the
  server with it (cached; reinstalls only when deps change);
- reads the dependency list from the server's PEP-723 `# /// script` block, so there is one
  source of truth for deps;
- fails loudly and actionably if neither uv nor python3+pip is usable — never a silent hang.

The only hard prerequisite is therefore **`python3` on PATH** (the launcher's interpreter
and the fallback's common denominator). `uv` is a speed bonus; `poppler` is a PDF-quality
bonus (the pymupdf/pypdf fallback chain covers extraction without it).

## Considered Options

- **uv-only** — simplest, no launcher to maintain, but a non-uv user must `pip install uv`
  first. Rejected as the *default* for a public audience; uv remains the preferred fast path.
- **Publish to PyPI, install via pipx** — the standard Python distribution path, but adds
  release/versioning overhead, pipx isn't universal, and it breaks the bundled-script model.
  **Deferred to the roadmap, not rejected** — it can coexist later as an alternate channel.

## Consequences

- First run on the fallback path is slower (one-time venv + pip install), cached thereafter.
- The venv lives under the Corpus home (`~/.rtfm/venv`), so deleting the home resets it too.
- A PyPI package as an additional install channel is tracked in [the roadmap](../ROADMAP.md).
