# OpenCode development notes

- The OpenCode front lives in `plugins/opencode/`.
- It installs from a local path or a git subdirectory; there is no OpenCode
  marketplace command equivalent to Claude's.
- Test it with a local `plugins` entry in a scratch `opencode.json`; the MCP
  server appears as `rtfm` and the skill as `read-the-manual`.
- Tools are `rtfm_*` (or `tools.rtfm.*` under Code Mode).
- Do not put shared skill content in `.opencode/skills/`; it is OpenCode-only.
