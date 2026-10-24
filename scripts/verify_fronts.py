#!/usr/bin/env python3
"""Verify both fronts against their pinned published core.

Network required: the front bootstrap resolves `rtfm-mcp==<version>` from PyPI.
This is an opt-in release check, not part of the hermetic test suite (ADR 0018).

For each front it:
  1. reads the version its own manifest pins,
  2. starts the front's bootstrap and speaks MCP over stdio,
  3. asserts the six public tools are registered,
  4. runs `claude plugin validate --strict` when the Claude CLI is on PATH.

The Claude real-session step needs a logged-in client and stays manual; see
docs/front-verification.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = {"find_duplicates", "health_check", "list_sources", "read", "reindex", "search"}
HANDSHAKE = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "verify-fronts", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


def front_version(front: str) -> str:
    """The version the front pins: package.json (OpenCode) or plugin.json (Claude)."""
    root = ROOT / "plugins" / front
    for path in (root / "package.json", root / ".claude-plugin" / "plugin.json"):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))["version"]
    raise SystemExit(f"ERROR: no manifest under {root}")


def tool_names(front: str) -> set[str]:
    """Start the front bootstrap and return the tools its MCP server registers."""
    root = ROOT / "plugins" / front
    payload = "\n".join(json.dumps(message) for message in HANDSHAKE) + "\n"
    with tempfile.TemporaryDirectory(prefix="rtfm-front-") as home:
        proc = subprocess.run(
            [sys.executable, str(root / "bootstrap.py"), "--front", front],
            input=payload, capture_output=True, text=True, timeout=600,
            env={**os.environ, "RTFM_HOME": home},
        )
    if proc.returncode != 0:
        raise SystemExit(f"ERROR: {front} bootstrap exited {proc.returncode}:\n{proc.stderr}")
    for line in proc.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 2:
            return {tool["name"] for tool in message["result"]["tools"]}
    raise SystemExit(f"ERROR: {front} bootstrap returned no tools/list response")


def validate_claude() -> bool:
    claude = shutil.which("claude")
    if claude is None:
        print("SKIP claude validate: the claude CLI is not on PATH")
        return True
    proc = subprocess.run(
        [claude, "plugin", "validate", str(ROOT / "plugins" / "claude"), "--strict"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
        return False
    print("OK   claude plugin validate --strict")
    return True


def main() -> int:
    ok = True
    for front in ("claude", "opencode"):
        version = front_version(front)
        names = tool_names(front)
        good = names == EXPECTED
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'} {front} {version}: {sorted(names)}")
    ok &= validate_claude()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
