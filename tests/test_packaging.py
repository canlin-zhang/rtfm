# tests/test_packaging.py
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_FRONT = ROOT / "plugins" / "claude"


def test_plugin_manifest_valid():
    m = json.loads((CLAUDE_FRONT / ".claude-plugin" / "plugin.json").read_text())
    assert m["name"] == "rtfm" and m["description"]


def test_claude_mcp_points_at_bootstrap():
    server = json.loads((CLAUDE_FRONT / ".mcp.json").read_text())["mcpServers"]["rtfm"]
    assert server["command"] == "python3"
    assert server["args"] == ["${CLAUDE_PLUGIN_ROOT}/bootstrap.py", "--front", "claude"]


def test_marketplace_source_points_at_claude_front():
    entries = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())["plugins"]
    assert [(p["name"], p["source"]) for p in entries] == [("rtfm", "./plugins/claude")]


def test_claude_skill_bundled_and_devendored():
    """The Claude front bundles the skill; every tool it names is rtfm's own (ADR 0008)."""
    text = (CLAUDE_FRONT / "skills" / "read-the-manual" / "SKILL.md").read_text()
    assert text.startswith("---")
    frontmatter = text.split("---", 2)[1]
    assert "name: read-the-manual" in frontmatter and "description:" in frontmatter
    refs = re.findall(r"mcp__\w+", text)
    # Both prefixes name rtfm's own server: the plugin install and a manual
    # server named `rtfm`. ADR 0008 forbids foreign tools, not these two.
    assert refs and all(
        ref.startswith(("mcp__plugin_rtfm_rtfm__", "mcp__rtfm__")) for ref in refs
    )


def test_shared_skill_has_no_client_tool_names():
    text = (ROOT / "skills" / "read-the-manual" / "SKILL.md").read_text()
    assert "mcp__" not in text
    assert "rtfm_" not in text
