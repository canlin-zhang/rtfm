# Verifying the fronts

Both fronts install the published `rtfm-mcp` core through their own bootstrap.
The structural tests pin the manifests, the bootstrap helpers, and the front
layout; this page covers the checks that need a real client or the published
package. Run them after a core release.

## Automated check

```bash
python3 scripts/verify_fronts.py
```

The script reads each front's pinned version, starts that front's `bootstrap.py`,
speaks MCP over stdio, and asserts the six public tools are registered. When the
`claude` CLI is on `PATH` it also runs `claude plugin validate --strict`.

Expected output (version follows the front manifests):

```
OK   claude 0.9.1: ['find_duplicates', 'health_check', 'list_sources', 'read', 'reindex', 'search']
OK   opencode 0.9.1: [...]
OK   claude plugin validate --strict
```

It needs network access, so it is deliberately outside the test suite (ADR
0018). A failure here means the pin, the bootstrap, or the published wheel is
broken, not that the code changed.

## OpenCode session

Point a scratch project at the local plugin and ask for a tool call:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/to/rtfm/plugins/opencode"]
}
```

```bash
opencode run --standalone --print-logs --log-level debug --auto \
  -m <provider/model> "Use the rtfm health_check tool and reply with its schema_version."
```

Expected in the log and output:

- `loading plugin ... /plugins/opencode/index.ts`
- `mcp connected server=rtfm tools=6`
- the tool runs through `tools.rtfm.health_check()`
- asking for the plugin's skills lists `read-the-manual`

## Claude Code session

Validation is automated (above). The real-session step needs a logged-in
client:

```bash
claude plugin validate plugins/claude --strict
claude plugin marketplace add /absolute/path/to/rtfm
# then install `rtfm` from the marketplace in a session
```

In the session, ask for `health_check` and confirm the `rtfm` tools are present;
the bundled skill is `read-the-manual`.

The validator also reports a directory-profile warning when the plugin has no
icon (`ICON_MISSING`). That is a listing concern, not a runtime failure; add a
square PNG at `plugins/claude/.claude-plugin/icon.png` if the plugin is
submitted to the directory.
