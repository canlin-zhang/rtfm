---
status: accepted
---

# git_repo source type: git-tracked document sources

The `dir` source type indexes a path — when that path is a git checkout, the
branch is invisible to rtfm. Nothing in a query result says which ref the
content came from, and nothing prompts a refresh. A stale checkout is
indistinguishable from a document that says nothing, and the failure is silent
in the worst direction: the query *succeeds*, returning confident, cited results
from old content.

This bit us for real (rtfm#9): a four-month-stale `master` checkout hid a
response-translation table that lived on a dev branch. The table existed. rtfm
answered exactly what it was asked; there was just no way to see the corpus was
old.

## Decisions

### Type name: `git_repo`, hosting-agnostic

`type = "git_repo"` — not `repo`, to leave room for other VCS backends and to
signal that this is specifically a git-tracked source. The `url` field is a
full remote URL — no implied GitHub, no default host.

Amends ADR 0002 (which used `type: "repo"`).

### ref: any git refspec, defaults to remote HEAD

`ref` is a branch name, tag, or full SHA. If omitted, rtfm resolves the
remote's default branch HEAD (the clone's remembered default — a local
symbolic-ref read, never a network call). A bare SHA means staleness is
undefined ("detached" state); branches and tags are compared against their
upstream.

### Two modes: linked (path provided) and managed (path omitted)

- **Linked** — `path` points to an existing clone the user owns. rtfm never
  mutates it (no fetch, no checkout). It verifies the remote URL matches and
  the tree is clean before indexing.
- **Managed** — no `path`; rtfm clones to `~/.rtfm/repos/<name>/` and owns the
  lifecycle. Fetch + checkout on every `reindex`.

Amends ADR 0007 (shallow clone → full clone; see below).

### No shallow clone for managed

ADR 0007 specified `--depth 1`. A shallow clone breaks `git checkout` of older
refs that have fallen out of the shallow history. Full clone is simpler and
correct; disk cost is modest for document repos.

### Staleness: commit-based, checked on every search

Staleness means the indexed commit ≠ the upstream ref. On every `search`,
rtfm compares the commit stored in `source_meta` against the current ref
state (per mode below) and reindexes inline when stale — no staleness window,
no budget gate. The principle is "do the simple correct thing first, optimize
later." A failed fetch/reindex degrades gracefully: search the
currently-indexed content with a loud warning.

The git_repo verdicts cost git subprocesses (and a fetch for managed sources)
and feed the failure warning, which would otherwise repeat on every query for
a persistently broken or dirty source — they are memoized in-process per
source for a short window (STALENESS_TTL, 30 s). Staleness is therefore
checked at most every 30 s per source, and a recurring warning repeats at
most every 30 s, not on every query.

The comparison is per-mode, honoring the read-only linked contract:

- **Managed** — fetch first (rtfm owns the clone), then compare against
  `origin/<ref>`. A stale index is fixed by fetch + checkout + reindex.
- **Linked** — never fetch. The refreshable reality is the user's working tree,
  so staleness means the indexed commit ≠ the current HEAD (the user moved
  their checkout) *or the tree is dirty* (uncommitted edits would be silently
  absent from results; the auto-reindex refusal then warns loudly on search);
  reindex then picks up the new tree. A remote that moved without the user
  refreshing their clone is the user's own knowledge — rtfm cannot know it
  without fetching, and must not fetch. The `list_sources` status compares
  against the clone's *local* `origin/<ref>` refs, so a
  fetched-but-not-checked-out clone still reports "behind".
- **Pinned SHA** — the pin never moves: staleness is undefined. Once indexed,
  a SHA-pinned source is never auto-reindexed — with one exception: a managed
  pin whose *manifest* ref changed (the user pinned a new version) is detected,
  because the managed comparison is against the pin itself, not "never stale".
  Linked pins never auto-reindex: the tree is the user's checkout concern.

Amends ADR 0003 (staleness window → every-query).

### No `refresh` flag

ADR 0003 proposed a `refresh: true/false` toggle. Fetch-on-reindex is always-on;
a user who wants a pinned version sets `ref` to a tag or SHA. The flag is dead
code — dropped.

### Dirty tree = refuse

If `git status --porcelain` is non-empty, `reindex` refuses. The error message
names the dirty files and tells the caller what to do (commit, stash, clean).
Local edits silently becoming "what the spec says" is the same failure mode as
a stale checkout — confident, cited results from the wrong content.

The refusal is gated on staleness: `search` only reindexes a source it found
stale, and a dirty tree makes a linked source stale, so the first search after
a file is edited warns loudly (the reindex refusal) instead of serving silently
stale content. The warning recurs while the tree stays dirty — bounded by the
staleness memo (at most every 30 s per source) — until the tree is committed,
stashed, or cleaned.

### Git terminology in output, not invented terms

`list_sources` reports git's own status language: `"up to date"`, `"behind"`,
`"ahead"`, `"diverged"`, `"detached"`, `"dirty"`. "ahead"/"diverged" only
occur for linked clones with unpushed or diverged local work (managed clones
are always reset to origin). "detached" is git's own detached-HEAD state — a
pinned SHA, where staleness is undefined.

Three operational values sit outside git's vocabulary and are rtfm's own:
`"never indexed"` (no `source_meta` row yet), `"unknown"` (the declared ref
does not resolve — check the ref spelling in the manifest; for managed
sources also a failed fetch — check the network), and an `"error: ..."`
string (a git call failed; the detail names it). The git-native values are
git's; these three are rtfm's, and are documented as such.

### mutable is not applicable

`git_repo` sources ignore the `mutable` flag — for managed clones the truth is
the remote at `ref`, never local edits. (Linked clones index the user's tree
as-is, but never mutate it.)

### Timeout: 60s default, configurable

`git fetch` and `git clone` get a 60-second timeout, overridable via
`RTFM_GIT_TIMEOUT`. Based on common practice across GitLab, Forgejo, and Go
tooling. A timeout degrades gracefully (warn, search stale).

### source_meta table for git state

A new `source_meta(source TEXT PRIMARY KEY, git_commit TEXT,
git_commit_date TEXT)` table stores the indexed commit per source. This is
source-level metadata, not per-file — separate from the `locations` table.

### Schema version bump: 3 → 4

Same migration strategy: drop all tables, rebuild empty. The index is a cache;
every version bump is a clean-slate rebuild.

### Managed clone errors are typed and actionable

When a managed clone path exists but isn't a git repo, is the wrong repo, or
has a stale remote URL, rtfm refuses with a classified error message naming the
specific mismatch and the recovery action. Agents invoking rtfm can parse these.
A missing git binary is its own class (`ERROR:GIT_MISSING`), never misreported
as "not a git repo".

## Consequences

- `search`, `reindex`, `list_sources`, and `health_check` all gain `git_repo`
  awareness.
- `_stale_delta` becomes a polymorphic dispatch: file-based for `dir`,
  commit-based for `git_repo`.
- The `Source` dataclass drops `refresh` and gains `ref`; `url` moves from
  vestigial to operational.
- One new table, one schema version bump, zero backward-compatibility concerns
  (the index is a cache).
