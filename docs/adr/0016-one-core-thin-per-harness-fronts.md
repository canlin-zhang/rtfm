---
status: accepted
---

# One core, one thin front per harness; the core is published once

rtfm was packaged as a Claude Code plugin whose repository root *was* the
artifact: `.claude-plugin/`, `skills/`, `bin/launch`, and `rtfm_server.py` all
shared one root. OpenCode cannot install that artifact, and naively adding an
OpenCode plugin to the same root made each harness read the other's files:

- Claude Code reads `AGENTS.md` directly when no `CLAUDE.md` exists
  (v2.1.277+), so an `AGENTS.md` written for OpenCode becomes Claude's project
  instructions.
- OpenCode discovers `.claude/skills` and `.agents/skills`; Claude Code reads
  `.claude/skills` but never `.agents/`. A skill placed for one harness is
  loaded by the other, tool names included.
- Claude's marketplace copies one plugin directory and enforces containment;
  OpenCode's `plugin add` npm-packs one directory. Both pack rules would compete
  for the same root, and neither front can reference a sibling core.

Both harnesses support monorepo subdirectory installs — Claude's `git-subdir`
source and OpenCode's `::path:` selector — so splitting repositories is not
required. What is required is that the core exists once and each harness receives
only its own thin layer.

## Decision

**The repository stays a monorepo. The root is the core plus the development
environment; each installable artifact lives under `plugins/<harness>/`.**

```
/
├── rtfm_server.py              core, one copy
├── bin/launch                  core launcher (dev/source runs)
├── skills/read-the-manual/     shared skill rules, never auto-discovered
├── plugins/
│   ├── claude/
│   │   ├── .claude-plugin/plugin.json
│   │   ├── .mcp.json           python3 bootstrap.py
│   │   ├── bootstrap.py
│   │   └── skills/read-the-manual/SKILL.md
│   └── opencode/
│       ├── package.json
│       ├── index.ts            ctx.mcp.transform + ctx.skill.transform
│       ├── bootstrap.py
│       └── skills/read-the-manual/SKILL.md
├── pyproject.toml / uv.lock / tests/ / docs/
├── AGENTS.md                   shared rules only
└── CLAUDE.md                   Claude-only development notes
```

**The core is published once as `rtfm-mcp` on PyPI** (the name `rtfm` is taken;
the console script is `rtfm-server`). A front contains no `rtfm_server.py`. Its
bootstrap installs the pinned core into a harness-private, version-stamped venv
(`${CLAUDE_PLUGIN_DATA}/venv` for Claude, `~/.rtfm/fronts/<front>/venv` for
OpenCode) and runs `python -m rtfm_server` (amended by ADR 0019). It does not
resolve the package at launch. This is how a multi-harness MCP server keeps one
core and thin fronts.

**A front does registration and skill delivery only.** It owns its harness's MCP
entry and whatever that harness needs to load the skill. It never reimplements
or vendors core behaviour.

**Skills are front content with a shared rules source.** `skills/read-the-manual/`
holds the harness-neutral rules (no knowledge fallback, citation format, missing
references). Each front's copy may differ only in its tool names and trigger
section, because Claude exposes `mcp__plugin_rtfm_rtfm__<tool>` and OpenCode
exposes `rtfm_<tool>` (or `tools.rtfm.<tool>` under Code Mode). CI compares the
shared portion so the copies cannot drift silently.

**Instruction files are split by discovery surface.** `AGENTS.md` contains only
rules true for both harnesses; Claude-specific notes live in `CLAUDE.md`;
OpenCode-specific notes live in a file referenced by `opencode.json`'s
`instructions`. Nothing harness-specific sits in a directory another harness
auto-discovers.

**Every version pin is declared once per artifact and checked mechanically.**
The release version appears in `pyproject.toml`, `uv.lock`, the Claude
`plugin.json`, and the OpenCode `package.json`; each bootstrap reads the
version from its own front manifest. `scripts/bump_version.py` updates them
together; the consistency check fails on drift.

## Considered Options

- **One shared artifact root for both harnesses** — technically workable: both
  ignore unknown files, and npm's `files` can trim OpenCode's package. Rejected
  because it is the arrangement that produced the observed cross-reading, and it
  forces two pack rules onto one root. Thin per-harness artifacts make the
  boundary explicit.
- **Split repositories per harness** — rejected. It does not remove the need for
  a core artifact (the front still must contain or fetch one), and adds
  cross-repository version sync. Hosting is cheap; sync is not.
- **Each front vendors a copy of the core** — rejected. Two copies of a 100 KB
  server drift, double every core diff, and contradict the single-core premise.
- **No PyPI; fronts fetch a pinned git tag at first run** — viable, but it
  reintroduces a bespoke download/verify layer in every front and still requires
  git. PyPI is the standard channel, is already on the roadmap, and keeps the
  install to `python3` + pip.

## Consequences

- A release now has a publish step (PyPI, via GitHub Actions Trusted Publishing)
  before the fronts can resolve their pinned core. A front released ahead of its
  core fails loudly at bootstrap instead of silently running another version.
- First run downloads the core; the launcher already downloads dependencies on
  first run, so the network requirement is unchanged.
- Each machine keeps two installed front copies and one shared Corpus home
  (`~/.rtfm`). Version skew between the two copies is now a first-class risk:
  the index schema migration is destructive, so two core versions can repeatedly
  drop each other's tables. That needs its own decision; this ADR only makes the
  skew possible, it does not solve it.
- `bin/` is no longer the plugin root's `bin/`, so Claude no longer adds a bare
  `launch` command to `PATH`. No launcher rename is needed.
- The launcher itself is unchanged (ADR 0009); only *where the server file comes
  from* changes. ADR 0009's uv-preferred/venv-fallback behaviour still governs
  running the installed core.
