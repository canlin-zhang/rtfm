# tests/test_packaging.py
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_valid():
    m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "rtfm" and m["description"]


def test_mcp_server_points_at_launcher():
    m = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    server = m["mcpServers"]["rtfm"]
    assert server["command"] == "python3"
    assert "${CLAUDE_PLUGIN_ROOT}/bin/launch" in server["args"]


def test_marketplace_lists_rtfm():
    m = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert any(p["name"] == "rtfm" for p in m["plugins"])


def test_read_the_manual_skill_bundled_and_devendored():
    """The plugin bundles the `read-the-manual` skill, de-vendored per ADR 0008."""
    skill = ROOT / "skills" / "read-the-manual" / "SKILL.md"
    assert skill.exists(), "read-the-manual skill not bundled in the plugin"
    text = skill.read_text()
    assert text.startswith("---")
    frontmatter = text.split("---", 2)[1]
    assert "name: read-the-manual" in frontmatter and "description:" in frontmatter
    # ADR 0008: every MCP tool the skill references must be rtfm's own (allowlist > denylist).
    tool_refs = re.findall(r"mcp__\w+", text)
    assert tool_refs, "skill references no MCP tools"
    assert all(ref.startswith("mcp__plugin_rtfm_rtfm__") for ref in tool_refs), tool_refs
    assert "mcp__plugin_rtfm_rtfm__search" in text and "mcp__plugin_rtfm_rtfm__read" in text
