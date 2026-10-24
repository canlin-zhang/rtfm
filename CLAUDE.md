@AGENTS.md

## Claude Code

- The Claude front lives in `plugins/claude/`; validate it with
  `claude plugin validate ./plugins/claude`.
- The plugin skill is `/rtfm:read-the-manual`, not `/read-the-manual`.
- Do not add project skills under `.claude/skills/`; OpenCode discovers that
  directory too, so shared content belongs in `skills/` or a front.
