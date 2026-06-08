# tests/test_packaging.py
import json
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
