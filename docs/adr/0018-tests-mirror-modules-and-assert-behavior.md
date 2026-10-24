---
status: accepted
---

# Tests mirror the core modules and assert behavior through public surfaces

The core is splitting into modules (ADR 0017). The existing suite is 5,821
lines across eleven files, imports one 2,223-line module, calls private helpers
directly, and sometimes spies on call sequences. That couples tests to the
implementation's shape: a pure refactor can break them, and a reader cannot tell
which module owns which test. The suite also has three oversized files
(`test_git_ops` 1,533 lines, `test_version_consistency` 1,218, `test_index`
1,172) whose boundaries no longer match the code.

This ADR follows *Software Engineering at Google*, chapter 12 ("Unit Testing")
and chapter 13 ("Test Doubles"): test via public APIs, test state rather than
interactions, keep tests unchanging across refactors, keep them clear and DAMP,
and use real local dependencies where they are fast and deterministic.

## Decision

**Layout and ownership.** One `tests/test_<module>.py` per `rtfm/<module>.py`.
Cross-module behavior gets named exceptions: `test_repo_sources.py` (git source
lifecycle), `test_search_quality.py`, and `test_e2e.py`. `test_facade.py` pins
the `rtfm_server` contract. The version-script tests split into
`test_check_version_consistency.py` and `test_bump_version.py` with one shared
fixture helper. `test_git_ops` keeps its primitive tests and its name; its
source-lifecycle tests move to `test_repo_sources.py`. `test_index` splits into
`test_extract.py`, `test_staging.py`, and `test_index.py`; `test_tools` into
`test_query.py` and a small `test_tools.py` that pins the MCP registration.

**Import surface.** Unit tests import the owning `rtfm.<module>`. They do not
use `rtfm_server` as a proxy for internals. `test_facade.py` is the only test
that treats `rtfm_server` as the unit: it pins the six public tools, `mcp`,
`main`, and the console entry point. `test_e2e.py` goes through the facade or
the server because that is the user-facing path.

**Public surfaces.** Each module declares a small public surface: functions
used by other modules or by tests drop the leading underscore; true internals
keep it. Tests call the public surface; private helpers are covered through it.
A pure, stable algorithm (for example FTS query sanitization or snippet
selection) may be tested directly when a public path would obscure the failure.

**State, not interactions.** Assert the final state: index rows,
`Indexed`/`CacheStats`, reports, returned `Freshness`. Call counts are allowed
only where the interaction *is* the contract: at most one fetch per staleness
window, one extraction per content, and no ingest after a read failure. Failure
injection uses a fake at a public seam, not a spy on a private call sequence.

**Size.** Sizes are documented, not mechanized. *Small* is pure Python with no
filesystem, network, or clock. *Medium* uses a temporary filesystem, SQLite, or
a local bare git remote. *Large* runs the MCP server over stdio. Most module
tests are narrow-scope and medium-sized; `test_e2e.py` is the large test. There
are no size markers or size-based directories; the module mirror is the primary
axis.

**Clarity.** Test names describe behavior (`subject_condition_expected` or
`should_..._when_...`); method- and script-oriented names are renamed during the
split. Use given/when/then structure where it helps, keep straight-line test
code with no logic of its own, and write assertions whose failure message names
the expected and actual state. Prefer DAMP over DRY: a little duplication is
fine when it keeps the test readable.

**Fixtures and builders.** Cross-module fixtures (`home`, git identity, sample
documents) stay in `tests/conftest.py`. Module-specific builders live in the
module's test file. A builder shared by two files moves to `tests/helpers/`.
Builders take the value under test explicitly and default only irrelevant
fields.

**Hermeticity and coverage.** Tests use temporary directories and local bare
git remotes; no network and no wall-clock sleeps. Coverage is not a gate; the
goal is meaningful coverage of each module's public surface plus the end-to-end
path. The suite stays fast enough to run on every change.

## Considered Options

- **Keep `rtfm_server` as the only test import.** Rejected: it keeps tests
  coupled to a facade and hides module ownership, the exact coupling this split
  exists to remove.
- **Behavior-first files only.** Rejected: behavior names are the right unit of
  assertion, but without module ownership the large files keep growing and a
  reader cannot find the tests for a module.
- **Test-pyramid directories (`tests/unit`, `tests/integration`, `tests/e2e`).**
  Rejected: size is documented, not a directory axis; the module mirror is more
  useful for ownership, and the behavior exceptions already mark the broad tests.
- **Keep the current interaction tests and only retarget them.** Rejected: they
  are the brittle tests the reference warns about; state assertions and a few
  contract-specific counts replace them.

## Consequences

- The split moves and renames tests. That is intentionally a large diff; it is
  mechanical and the full test suite is the gate.
- The eight monkeypatch sites retarget to public seams or become state tests.
- The PEP-723 mirror tests disappear with the single dependency source; the
  version tests keep only the four version declarations.
- `rtfm_server` becomes a small compatibility surface, not the test API, so a
  future change to the console shim cannot silently alter module behavior.
