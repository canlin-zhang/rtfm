---
status: accepted
---

# Manifest is a single TOML file under a flat ~/.rtfm/ home

All of the tool's state lives under one Corpus home, env-overridable via `RTFM_HOME`,
defaulting to `~/.rtfm/`:

- `manifest.toml` — the Source declarations, one `[[source]]` block each.
- `default/` — the Default source's `mutable` drop-dir.
- `cache/*.db` — the FTS indexes.

The Manifest is **TOML**: human- and agent-editable, supports inline comments (the shipped
`manifest.example.toml` documents each field), and avoids YAML's whitespace fragility for
the hand edits that ADR 0001 makes load-bearing (editing the Manifest is the consent gate).
On first run with no Manifest, the server creates the home, writes a Manifest containing
only the Default source, and creates `default/` — so dropping a file in
`~/.rtfm/default/` makes search work with zero configuration.

## Considered Options

- **YAML manifest** — rejected: more ubiquitous, but whitespace-fragile for hand edits, and
  consent-by-edit (ADR 0001) means humans edit it often.
- **Strict XDG layout** (`~/.config` + `~/.local/share` split) — rejected: the "correct"
  Linux layout, but it scatters config, drop-dir, and cache across three trees. One flat
  home is easier for a human to reason about, back up, and delete. *Deliberate deviation —
  do not "fix" to XDG.*

## Consequences

- Backing up, inspecting, or deleting one directory resets the whole tool.
- `RTFM_HOME` relocates everything (e.g. onto a larger volume) with one env var.
- User state lives in `$HOME`, never in the checkout, so the published repo carries no user
  data — it ships only `manifest.example.toml`. The repo `.gitignore` still covers `*.db`
  and `__pycache__/` for anyone running from a dev checkout.
- Source `name`s must be unique (derived from the path/url basename when omitted). On a
  collision the first wins, the duplicates are refused, and `health_check`/`list_sources`
  carry a loud warning naming the conflicting paths and suggesting version-stamped names
  (`vendor-docs-2025.06`) — never a silent merge, silent rename, or hard server failure.
