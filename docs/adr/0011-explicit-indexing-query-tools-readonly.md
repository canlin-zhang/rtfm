---
status: accepted
---

# Indexing: explicit reindex + bounded auto-reindex on query

`reindex` is the sole *unbounded* builder. `search` additionally keeps the corpus current with a
**bounded auto-reindex**: before searching, each dir source touched by the query gets a cheap
`(relpath, mtime)` staleness scan (no extraction); a source within the auto-reindex budget
(`RTFM_AUTO_REINDEX_MAX`, default 10 new/changed files) is reindexed inline, while a larger delta
is left to an explicit `reindex` and reported in `WARNING` rather than blocking the query on
extraction. `list_sources` and `health_check` stay query-only. Auto-reindex refreshes the search
cache only — it never mutates source files (the `mutable` flag gates file mutation, Plan 4, not
freshness).

## Considered Options

- **Auto-index on query (inherited, unbounded).** Every `search`/`list` rebuilt synchronously.
  Rejected originally: a first search over a 100+ PDF source blocked past usable latency / MCP
  timeouts.
- **Fully explicit (no auto-index anywhere).** Rejected: users silently add or edit files and
  expect search to reflect them; forcing a manual `reindex` after every change is a footgun, and
  a never-indexed source silently returns nothing.
- **Explicit + mutable-only self-heal (0.2.1–0.3.0).** Only `mutable` dir sources self-healed.
  Superseded: tied freshness to a flag whose real job is file-mutation gating, and left
  read-only sources stale after their upstream changed.
- **Explicit + bounded auto-reindex on query (chosen, 0.4.0).** Every dir source auto-reindexes
  within a file-count budget; larger deltas warn instead of blocking. Keeps the drop-and-search
  loop for small sources, reflects edits to any source, and bounds query latency.

## Why the budget

Auto-index was originally removed because synchronous extraction blocked the server. With the
0.2.1 ThreadPool fix the failure mode is *latency*, not a hang, so a small inline reindex is
safe. The budget caps that latency: PDF extraction dominates cost, so a per-source new/changed
file count is the cheap, honest proxy. `RTFM_AUTO_REINDEX_MAX=0` disables inline auto-reindex
(warn-only) for users who want strictly explicit builds.

## Consequences

- A query against a small never-indexed source now just works (auto-reindexed inline); a large
  one returns prior content (possibly `[]`) plus a loud WARNING to run `reindex` — never a silent
  empty result, never a blocking extraction.
- Misconfigured sources (missing / non-dir / unreadable path) are warned and skipped, not fatal;
  one bad manifest entry never breaks the others.
- Full non-blocking **async** reindex with partial results remains the scale-out path so no call
  blocks at any size.
