#!/usr/bin/env python3
"""Verify version and dependency declarations stay in sync across the repo.

The release version is declared in three files — pyproject.toml, uv.lock's rtfm
entry, and .claude-plugin/plugin.json — and the dependency list is mirrored
between pyproject.toml and the PEP-723 `# /// script` block in rtfm_server.py
(ADR 0009: pyproject mirrors the script block). Nothing mechanical links them,
so a one-file edit silently drifts (0.5.1: plugin.json/uv.lock lagged behind,
and uv.lock resolved mcp 1.27.2 below the >=1.28.1 pin, defeating the fix).
This script is that mechanical link: CI runs it on every push and
`scripts/bump_version.py` runs it after every bump.

Exit 0 = consistent, 1 = drift found.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def _read_pep723_deps(server: Path) -> list[str]:
    lines = server.read_text().splitlines()
    start = next((i for i, line in enumerate(lines) if line == "# /// script"), None)
    end = next((i for i, line in enumerate(lines) if line == "# ///"), None)
    if start is None or end is None or end <= start:
        sys.exit(f"ERROR: {server}: PEP-723 block (# /// script ... # ///) not found")
    block = "\n".join(line.removeprefix("# ") for line in lines[start + 1 : end])
    return tomllib.loads(block)["dependencies"]


def main() -> int:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())

    pkg_version = pyproject["project"]["version"]
    lock_version = next(p["version"] for p in lock["package"] if p["name"] == "rtfm")
    plugin_version = plugin["version"]

    versions = {
        "pyproject.toml": pkg_version,
        "uv.lock (rtfm entry)": lock_version,
        ".claude-plugin/plugin.json": plugin_version,
    }
    if len(set(versions.values())) > 1:
        for name, v in versions.items():
            print(f"  {name}: {v}")
        return _fail("release version differs across files; run scripts/bump_version.py")

    script_deps = set(_read_pep723_deps(ROOT / "rtfm_server.py"))
    project_deps = set(pyproject["project"]["dependencies"])
    if script_deps != project_deps:
        for extra in sorted(script_deps - project_deps):
            print(f"  only in rtfm_server.py PEP-723 block: {extra}")
        for missing in sorted(project_deps - script_deps):
            print(f"  only in pyproject.toml: {missing}")
        return _fail("dependency declarations differ (mcp pin drift?); mirror the change in both")

    print("OK: version and dependency declarations are consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
