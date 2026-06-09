# Installing rtfm for OpenCode

## Prerequisites

- [OpenCode.ai](https://opencode.ai) installed
- `python3` on PATH

## Installation

Add rtfm to the `plugin` array in your `opencode.json` (global or project-level):

```json
{
  "plugin": ["rtfm@git+https://github.com/canlin-zhang/rtfm.git"]
}
```

Restart OpenCode. The plugin installs through OpenCode's plugin manager,
registers the MCP server, and discovers the `read-the-manual` skill.

## Usage

Drop PDFs, `.md`, `.rst`, or `.txt` files into `~/.rtfm/default/`, then ask
OpenCode to search them:

```
/read-the-manual: what does the spec say about timeouts?
```

Or invoke tools directly: `search`, `read`, `reindex`, `find_duplicates`,
`list_sources`, `health_check`.

## Updating

To pin a specific version:

```json
{
  "plugin": ["rtfm@git+https://github.com/canlin-zhang/rtfm.git#v0.6.0"]
}
```

## Troubleshooting

### Plugin not loading

1. Verify `python3` is on PATH: `python3 --version`
2. Check the plugin line in your `opencode.json`
3. Make sure you're running a recent version of OpenCode

### Skills not found

Use the `skill` tool to list what's discovered. If `read-the-manual` is
missing, restart OpenCode and check that the plugin loaded.

### MCP server won't start

The server needs `python3`. If uv is installed it's used automatically for
the fast path.

```
python3 bin/launch  # test the launcher directly from the rtfm repo
```

## Getting Help

- Report issues: https://github.com/canlin-zhang/rtfm/issues
- rtfm docs: https://github.com/canlin-zhang/rtfm
