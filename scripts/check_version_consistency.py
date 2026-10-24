#!/usr/bin/env python3
"""Verify the version declarations stay in sync across the repo.

The release version is declared in four files — pyproject.toml, uv.lock's
rtfm-mcp entry, plugins/claude/.claude-plugin/plugin.json, and
plugins/opencode/package.json. Dependencies have one source: the
[project].dependencies list in pyproject.toml (ADR 0017). Nothing mechanical
linked the version values, so a one-file edit silently drifted (0.5.1: the
Claude manifest moved first while pyproject.toml and uv.lock lagged).

Coverage, precisely: this script compares the declared version strings. It does
not read uv.lock's resolved versions; a lock that resolves a dependency outside
its declared constraint is caught by `uv lock --check`, which the CI job runs
alongside this script.

Exit 0 = consistent, 1 = drift found. The version comparison is exact-string;
the dependency comparison is set-based (duplicate entries and ordering within
a list are invisible to it).
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


def _read_text(path: Path) -> str:
    """Read a repo file, or exit with a clean error instead of a traceback."""
    try:
        # The repo files are UTF-8 by contract; an explicit encoding keeps the
        # failure message honest under ASCII locales.
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.exit(f"ERROR: {path} not found")
    except OSError as e:
        sys.exit(f"ERROR: {path}: {e}")
    except UnicodeDecodeError as e:
        sys.exit(f"ERROR: {path} not UTF-8 text: {e}")


def _read(name: str, loader: Callable[[str], dict]) -> dict:
    """Read + parse a config file, or exit with a clean error instead of a traceback."""
    try:
        return loader(_read_text(ROOT / name))
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as e:
        sys.exit(f"ERROR: {name} unparseable: {e}")


def _key(name: str, container: object, *keys: str):
    """Descend into a parsed config, or exit cleanly on a missing key or a
    non-table value (a wrong-typed container parses fine — `project = "x"` is
    valid TOML — so the message names both failure shapes)."""
    for k in keys:
        if not isinstance(container, dict) or k not in container:
            sys.exit(f"ERROR: {name}: missing key '{'.'.join(keys)}' or non-table value")
        container = container[k]
    return container


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # The drift listing can print non-ASCII version values; an ASCII
        # stdout must not kill the report (the bump has the same guard).
        sys.stdout.reconfigure(errors="replace")
    pyproject = _read("pyproject.toml", tomllib.loads)
    lock = _read("uv.lock", tomllib.loads)
    plugin = _read("plugins/claude/.claude-plugin/plugin.json", json.loads)
    opencode = _read("plugins/opencode/package.json", json.loads)

    pkg_version = _key("pyproject.toml", pyproject, "project", "version")
    lock_packages = _key("uv.lock", lock, "package")
    plugin_version = _key("plugins/claude/.claude-plugin/plugin.json", plugin, "version")
    opencode_version = _key("plugins/opencode/package.json", opencode, "version")

    # The descent above guarantees the keys exist, not that the values have the
    # right shape (a valid-TOML file with `package = "rtfm"` parses fine). Each
    # guard turns a would-be AttributeError/TypeError into a clean drift error.
    if not isinstance(lock_packages, list) or not all(isinstance(p, dict) for p in lock_packages):
        return _fail("uv.lock: [[package]] must be an array of tables")
    rtfm_entries = [p for p in lock_packages if p.get("name") == "rtfm-mcp"]
    if not rtfm_entries:
        return _fail("uv.lock has no [[package]] entry named 'rtfm-mcp'")
    if len(rtfm_entries) > 1:
        return _fail(f"uv.lock has {len(rtfm_entries)} [[package]] entries named 'rtfm-mcp'")
    lock_version = rtfm_entries[0].get("version")
    if lock_version is None:
        return _fail("uv.lock: [[package]] entry named 'rtfm-mcp' has no version key")

    versions = {
        "pyproject.toml": pkg_version,
        "uv.lock (rtfm-mcp entry)": lock_version,
        "plugins/claude/.claude-plugin/plugin.json": plugin_version,
        "plugins/opencode/package.json": opencode_version,
    }
    bad_versions = [
        name for name, v in versions.items() if not (isinstance(v, str) and v.strip())
    ]
    if bad_versions:
        # Non-empty (after stripping) too: all-empty or whitespace-only versions
        # would pass the isinstance guard and set-equality as 'OK', blessing a
        # wiped version field.
        return _fail(
            "release version must be a non-blank string in every file"
            f" (offending: {', '.join(bad_versions)})"
        )
    if len(set(versions.values())) > 1:
        for name, v in versions.items():
            print(f"  {name}: {v}")
        return _fail(
            "release version differs across files; run scripts/bump_version.py once "
            "pyproject.toml and the front manifests agree (it refuses while they disagree), "
            "or align the versions by hand"
        )

    # scripts/bump_version.py's self-check gate matches on this exact line —
    # keep the two in sync.
    print("OK: version declarations are consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
