#!/usr/bin/env python3
"""Verify version and dependency declarations stay in sync across the repo.

The release version is declared in three files — pyproject.toml, uv.lock's rtfm
entry, and .claude-plugin/plugin.json — and the dependency list is mirrored
between pyproject.toml and the PEP-723 `# /// script` block in rtfm_server.py,
which is the source of truth for deps (ADR 0009; see the header comment in
pyproject.toml). Nothing mechanical linked them, so a one-file edit silently
drifted (0.5.1: plugin.json/uv.lock lagged at 0.5.0, and the PEP-723 block lost
its mcp pin while pyproject.toml gained one).

Coverage, precisely: this script compares *declared* strings — the version
values and the dependency specifiers as written. It does not read uv.lock's
resolved versions; a lock that resolves a dependency outside its declared
constraint (0.5.1: mcp 1.27.2 below the >=1.28.1 pin) is caught by `uv lock
--check`, which the CI job runs alongside this script.

Exit 0 = consistent, 1 = drift found.
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def _read(name: str, loader: Callable[[str], dict]) -> dict:
    """Read + parse a config file, or exit with a clean error instead of a traceback."""
    path = ROOT / name
    try:
        return loader(path.read_text())
    except FileNotFoundError:
        sys.exit(f"ERROR: {name} not found")
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as e:
        sys.exit(f"ERROR: {name} unparseable: {e}")


def _read_pep723_deps(server: Path) -> list[str]:
    try:
        lines = server.read_text().splitlines()
    except FileNotFoundError:
        sys.exit(f"ERROR: {server} not found")
    start = next((i for i, line in enumerate(lines) if line == "# /// script"), None)
    end = next((i for i, line in enumerate(lines) if line == "# ///"), None)
    if start is None or end is None or end <= start:
        sys.exit(f"ERROR: {server}: PEP-723 block (# /// script ... # ///) not found")
    block = "\n".join(line.removeprefix("# ") for line in lines[start + 1 : end])
    try:
        return tomllib.loads(block)["dependencies"]
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"ERROR: {server}: PEP-723 block unparseable: {e}")


def main() -> int:
    pyproject = _read("pyproject.toml", tomllib.loads)
    lock = _read("uv.lock", tomllib.loads)
    plugin = _read(".claude-plugin/plugin.json", json.loads)

    pkg_version = pyproject["project"]["version"]
    lock_version = next(
        (p["version"] for p in lock["package"] if p["name"] == "rtfm"), None
    )
    if lock_version is None:
        return _fail("uv.lock has no [[package]] entry named 'rtfm'")
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
