---
status: accepted
---

# Repo sources: linked (path) vs managed (url), a provenance-only distinction

A `repo` Source is declared one of two ways:

- **Linked** — the user gives a `path` to an existing git working tree they own.
- **Managed** — the user gives a `url` and no `path`; the tool shallow-clones it
  (`--depth 1`) into `<corpus home>/repos/<name>/` on first use and keeps it current.

If both are given, `path` wins.

The two differ **only in provenance** — who created the checkout. After creation they are
treated identically: same indexing, same Refresh (`git pull --ff-only`, staleness-bounded,
per ADR 0003), and the same cause-distinguishing fail-soft warning on any non-clean state.
A tool-created clone is *not* assumed pristine: a checkout can be dirtied or diverged by
anything (a stray manual commit, another process, filesystem trouble), so it earns the same
rigor as a user's — never a blind `reset --hard`.

## Considered Options

- **Hard-reset managed clones to upstream on Refresh** (treat them as disposable mirrors) —
  rejected. It assumes a tool-owned clone is always pristine; it isn't, because people do
  arbitrary things to any git tree. A blind reset could destroy state and would *mask* the
  very divergence the fail-soft warning exists to surface. Uniform rigor is simpler and
  safer than a provenance-special-cased refresh.

## Consequences

- One Refresh code path serves both flavors; "managed vs linked" affects only first use
  (clone-from-url vs use-existing-path) and cleanup.
- Managed clones live under the Corpus home; removing the Source from the Manifest orphans
  the clone there (a future `prune`/health-check can report orphans).
- Private `url`s rely on the user's existing git credential helper / SSH; a clone failure
  degrades loudly, like any Refresh failure.
