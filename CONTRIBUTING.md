# Contributing to rtfm

Thanks for your interest! rtfm is a small, deliberately-scoped tool; contributions that keep
it focused and domain-neutral are very welcome.

## Development setup

```bash
git clone https://github.com/canlin-zhang/rtfm
cd rtfm
uv run pytest -q        # uv bootstraps all deps from pyproject.toml — no manual install
uvx ruff check .        # lint; uvx ruff check --fix . to autofix
```

Only `python3` is strictly required; `uv` makes everything one command. There is no separate
install step for development.

## Workflow

- **`main` is protected — open a pull request; no direct pushes.** Branch off `main`, push
  your branch, open a PR. CI (tests + ruff) must pass before merge.
- Keep one logical change per PR. Write the *why* in the PR description.
- Follow test-driven development: a failing test first, then the minimal code to pass it.

## Conventions

All coding conventions, the single-file constraint, and the design invariants live in
[AGENTS.md](AGENTS.md) — please read it. The short version: keep `rtfm_server.py` a single
file with thin MCP wrappers; comment the *why*; loud, actionable errors; `snake_case`; type
hints on public signatures; ruff-clean.

## Design changes

rtfm is ADR-driven. If you're changing behavior, read the relevant decision in
[`docs/adr/`](docs/adr/) first. For anything consequential or hard to reverse, add a new
numbered ADR (`docs/adr/NNNN-slug.md`) in your PR explaining the decision and the
alternatives you rejected. Domain vocabulary goes in [`CONTEXT.md`](CONTEXT.md).

## Scope guardrail

rtfm is domain-neutral by design. Do **not** add vendor-, project-, or domain-specific tables
or heuristics to shipped code or skills (see [ADR 0008](docs/adr/0008-skills-corpus-general-no-vendor-tables.md)).
Specialized knowledge belongs in the user's own private config.

## License

By contributing, you agree your contributions are licensed under the project's
[MIT License](LICENSE).
