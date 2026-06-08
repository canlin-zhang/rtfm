---
status: accepted
---

# Explicit indexing; query tools never rebuild

`reindex` is the sole builder. `search`, `list_sources`, and `health_check` are query-only and
never extract — except that `search` cheaply self-heals the mutable `default` drop-dir via a
`(relpath, mtime)` staleness check. This replaces the prior behavior where every query ran a
synchronous full index, which blocked the first search over a large source until all files were
extracted.

## Considered Options

- **Auto-index on query (inherited).** Every `search`/`list` rebuilt synchronously. Rejected: a
  first search over a 100+ PDF source blocked past usable latency / MCP timeouts.
- **Fully explicit (no auto-index anywhere).** Rejected: regresses the drop-and-search loop for
  the zero-config `default` dir, which is meant to be small.
- **Explicit, with default self-heal (chosen).** Encodes the contract: `default` = quick-and-dirty
  scratch (drop & search); pointed-to folders = an organized corpus managed via `reindex`.

## Consequences

- A query against a never-indexed pointed-to source returns `[]` and a loud WARNING to run
  `reindex`, never a silent empty result.
- Phase 2 will make `reindex` async with partial results so no call blocks at any scale.
