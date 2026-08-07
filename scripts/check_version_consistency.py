#!/usr/bin/env python3
"""Verify version and dependency declarations stay in sync across the repo.

The release version is declared in three files — pyproject.toml, uv.lock's rtfm
entry, and .claude-plugin/plugin.json — and the dependency list is mirrored
between pyproject.toml and the PEP-723 `# /// script` block in rtfm_server.py,
which is the source of truth for deps (ADR 0009; see the header comment in
pyproject.toml). Nothing mechanical linked them, so a one-file edit silently
drifted (0.5.1: the PEP-723 block gained the mcp pin in PR #11, plugin.json
moved to 0.5.1 in the same change, while pyproject.toml and uv.lock lagged
behind, and uv.lock still resolved mcp 1.27.2 below the pin).

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
    except OSError as e:
        sys.exit(f"ERROR: {name}: {e}")
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, UnicodeDecodeError) as e:
        sys.exit(f"ERROR: {name} unparseable: {e}")


def _key(name: str, container: object, *keys: str):
    """Descend into a parsed config, or exit cleanly if any key is missing."""
    for k in keys:
        if not isinstance(container, dict) or k not in container:
            sys.exit(f"ERROR: {name}: missing key '{'.'.join(keys)}'")
        container = container[k]
    return container


def _read_pep723_deps(server: Path) -> list[str]:
    try:
        lines = server.read_text().splitlines()
    except FileNotFoundError:
        sys.exit(f"ERROR: {server} not found")
    except OSError as e:
        sys.exit(f"ERROR: {server}: {e}")
    except UnicodeDecodeError as e:
        sys.exit(f"ERROR: {server}: not UTF-8 text: {e}")
    start = next((i for i, line in enumerate(lines) if line == "# /// script"), None)
    end = next((i for i, line in enumerate(lines) if line == "# ///"), None)
    if start is None or end is None or end <= start:
        sys.exit(f"ERROR: {server}: PEP-723 block (# /// script ... # ///) not found")
    block = "\n".join(line.removeprefix("# ") for line in lines[start + 1 : end])
    try:
        parsed = tomllib.loads(block)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"ERROR: {server}: PEP-723 block unparseable: {e}")
    return _key(str(server), parsed, "dependencies")


def main() -> int:
    pyproject = _read("pyproject.toml", tomllib.loads)
    lock = _read("uv.lock", tomllib.loads)
    plugin = _read(".claude-plugin/plugin.json", json.loads)

    pkg_version = _key("pyproject.toml", pyproject, "project", "version")
    project_deps = _key("pyproject.toml", pyproject, "project", "dependencies")
    lock_packages = _key("uv.lock", lock, "package")
    plugin_version = _key(".claude-plugin/plugin.json", plugin, "version")

    # The descent above guarantees the keys exist, not that the values have the
    # right shape (a valid-TOML file with `package = "rtfm"` parses fine). Each
    # guard turns a would-be AttributeError/TypeError into a clean drift error.
    if not isinstance(lock_packages, list) or not all(isinstance(p, dict) for p in lock_packages):
        return _fail("uv.lock: [[package]] must be an array of tables")
    lock_version = next((p.get("version") for p in lock_packages if p.get("name") == "rtfm"), None)
    if lock_version is None:
        return _fail("uv.lock has no [[package]] entry named 'rtfm'")

    versions = {
        "pyproject.toml": pkg_version,
        "uv.lock (rtfm entry)": lock_version,
        ".claude-plugin/plugin.json": plugin_version,
    }
    if not all(isinstance(v, str) for v in versions.values()):
        return _fail("release version must be a string in every file")
    if len(set(versions.values())) > 1:
        for name, v in versions.items():
            print(f"  {name}: {v}")
        return _fail("release version differs across files; run scripts/bump_version.py")

    # Guard against a bare-string deps list: set() would iterate its characters
    # and compare equal for any matching strings. uv itself rejects this, so the
    # guard is cheap insurance, not a reachable path. Element guards keep the
    # set/sorted comparisons from hitting mixed types.
    if not isinstance(project_deps, list) or not all(isinstance(d, str) for d in project_deps):
        return _fail("pyproject.toml: [project].dependencies must be a list of strings")
    script_deps = _read_pep723_deps(ROOT / "rtfm_server.py")
    if not isinstance(script_deps, list) or not all(isinstance(d, str) for d in script_deps):
        return _fail("rtfm_server.py: PEP-723 dependencies must be a list of strings")

    if set(script_deps) != set(project_deps):
        for extra in sorted(set(script_deps) - set(project_deps)):
            print(f"  only in rtfm_server.py PEP-723 block: {extra}")
        for missing in sorted(set(project_deps) - set(script_deps)):
            print(f"  only in pyproject.toml: {missing}")
        return _fail("dependency declarations differ (mcp pin drift?); mirror the change in both")

    print("OK: version and dependency declarations are consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
