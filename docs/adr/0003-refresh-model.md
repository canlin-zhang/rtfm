---
status: accepted
---

# Refresh is staleness-bounded and fast-forward-only, with cause-distinguishing failures

A Source Refreshes lazily: a `search`/`read` that touches it triggers a Refresh only when
the last Refresh is older than a configurable **staleness window** (default ~15 min);
otherwise the cached index is served. This pays the network/IO cost at most once per window
instead of once per query, and the policy generalizes unchanged to the planned Web source
(no per-query re-scrape). A per-Source flag `refresh: true` (default) can be set `false` to
**pin** a Source — e.g. a Repo checked out at a release tag.

For Repo sources, Refresh is `git pull --ff-only` — it never merges, stashes, or rewrites
the working tree. Because the tool treats a Repo source as read-only (ADR 0001), a
fast-forward failure can only mean the *user's own* checkout diverged from upstream, never
anything the tool did. A failed Refresh therefore serves the current on-disk state and
emits a loud, **cause-distinguishing** message that (a) names the specific reason,
(b) affirms the tool did not and will not modify the working tree, and (c) gives the
concrete recovery — never a generic "pull failed."

## Considered Options

- **Refresh every query** — rejected: per-query network latency, offline-fragile.
- **Explicit-refresh-only** — rejected: stale by default, defeats "latest when checking."
- **Auto-stash / auto-merge on divergence** — rejected: mutates the user's working tree
  behind their back, colliding with ADR 0001's read-only ethos.

## Consequences

- The planned Web source reuses this exact policy; only the Refresh implementation differs
  (re-fetch vs `git pull`).
- Failure causes a Repo Refresh must distinguish in its message: diverged local commits,
  dirty working tree, detached HEAD / no upstream, auth failure, network failure.
- The per-Source flag is named `refresh:` (not `pull:`) to match the polymorphic term.
