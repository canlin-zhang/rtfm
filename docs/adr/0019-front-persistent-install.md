---
status: accepted
---

# Fronts install the core into a persistent stamped venv

## Context

0.8.0 made the core a published wheel and the fronts thin. The front bootstrap
ran `uvx --from rtfm-mcp==<version> rtfm-server` at every session start. `uvx`
is a cached tool run, not a persistent install: it resolves the exact version on
every launch, keeps the environment in uv's cache, and has no stamp the front
controls. In a real Claude session this failed twice: a directory-name check
rejected the marketplace layout (fixed in 0.9.1), and uv's cached index did not
contain a just-published version, so the server never started. Forcing
`--refresh-package` fixed the second but turned a 0.2 s update into a 72 s
network-bound one.

## Decision

The front installs the pinned core once into a private, version-stamped venv and
execs its interpreter:

- `data_dir(front)/venv`, stamped by `.core-version`.
- `uv venv --seed --python >=3.11` + `uv pip install` when uv is present;
  `python3 -m venv` + pip otherwise.
- The first install attempt uses the cached index. Only a failure retries with
  `--refresh-package rtfm-mcp`, so a just-published version is found without a
  refresh on every update.
- A venv that has `bin/python` but no pip is rebuilt when uv is absent, and a
  failed install never writes the stamp.
- An `fcntl` lock serializes concurrent installs.
- The launch command is `venv/bin/python -m rtfm_server`: no resolver, no
  network.

This ADR amends the front-launch text in ADR 0009 and ADR 0016. uv-created venvs
are created with `--seed`, so the pip fallback remains usable if uv is later
removed.

## Considered options

- **Keep `uvx`.** Rejected: per-launch resolution and the negative cache are the
  defect, not a tuning problem. Refreshing on every launch is network-bound and
  breaks offline use.
- **Bundle the core with the plugin.** Guarantees startup inside the client's
  timeout, but native wheels make it a per-platform artifact; a separate design.
- **pip/venv only.** The standard baseline, but requires `python3-venv`, which
  some hosts lack; uv creates the venv without it.

## Consequences

- Updates install only the new core wheel; the dependency set is installed once.
- A launch after the stamp is offline and does not call uv.
- A machine that has never installed the core still downloads the dependencies
  on first launch. On a slow host that can exceed the client's 30 s startup
  timeout, so the first session needs one retry.
- The front's data dir now carries the venv; removing it forces a clean
  reinstall.
