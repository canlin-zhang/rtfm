# tests/helpers/version_tree.py
"""Shared fixture builders for the version check/bump contract tests."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


check = _load("check_version_consistency", ROOT / "scripts/check_version_consistency.py")


def make_tree(
    tmp_path,
    version="0.5.1",
    plugin_version=None,
    opencode_version=None,
    lock_version=None,
    lock_names=("rtfm-mcp",),
):
    """A minimal repo skeleton: the files the guard reads, all consistent."""
    plugin_version = version if plugin_version is None else plugin_version
    opencode_version = version if opencode_version is None else opencode_version
    lock_version = version if lock_version is None else lock_version
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "rtfm-mcp"\nversion = "{version}"\ndependencies = [\n'
        '    "mcp[cli]>=1.28.1,<2",\n    "pymupdf",\n    "pypdf",\n]\n',
        encoding="utf-8",
    )

    def lock_entry(name: str) -> str:
        entry_version = lock_version if name == "rtfm-mcp" else "0.1.0"
        return (
            f'[[package]]\nname = "{name}"\nversion = "{entry_version}"\n'
            'source = { virtual = "." }\n'
        )

    (tmp_path / "uv.lock").write_text(
        "".join(lock_entry(name) for name in lock_names), encoding="utf-8"
    )
    (tmp_path / "plugins/claude/.claude-plugin").mkdir(parents=True)
    (tmp_path / "plugins/claude/.claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "rtfm", "version": plugin_version}, indent=2) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "plugins" / "opencode").mkdir(parents=True)
    (tmp_path / "plugins" / "opencode" / "package.json").write_text(
        json.dumps(
            {
                "name": "rtfm-opencode-plugin",
                "version": opencode_version,
                "type": "module",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # The bump's self-check runs the check script; the skeleton must mirror it.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_version_consistency.py").write_text(
        "# stub\n", encoding="utf-8"
    )


def run_check(tmp_path, monkeypatch):
    """Run the check script against a fixture tree."""
    monkeypatch.setattr(check, "ROOT", tmp_path)
    return check.main()


def _run_script_in_subprocess(
    script_path,
    module_name,
    tmp_path,
    path_extra=None,
    env_extra=None,
    timeout=10,
    require_ascii=False,
):
    """Run a real guard script against a fixture tree in a child process."""
    if require_ascii and (
        sys.platform != "linux" or sys.getfilesystemencoding() != "utf-8"
    ):
        pytest.skip("require_ascii pins need a Linux UTF-8 host")
    loader = (
        "import importlib.util, sys; from pathlib import Path; "
        "assert sys.argv[3] == '0' or sys.getfilesystemencoding() == 'ascii', "
        "'env_extra must reach the child'; "
        f"s = importlib.util.spec_from_file_location({module_name!r}, sys.argv[1]); "
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "m.ROOT = Path(sys.argv[2]); "
        f"sys.argv = [{module_name!r}, '0.5.2']; sys.exit(m.main())"
    )
    env = dict(os.environ)
    if path_extra:
        env["PATH"] = path_extra + env["PATH"]
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            loader,
            str(script_path),
            str(tmp_path),
            "1" if require_ascii else "0",
        ],
        timeout=timeout,
        capture_output=True,
        text=True,
        errors="replace",
        env=env,
    )


def run_check_in_subprocess(tmp_path, env_extra=None, timeout=10, require_ascii=False):
    """Run the real check script against a fixture tree in a child process."""
    return _run_script_in_subprocess(
        ROOT / "scripts/check_version_consistency.py",
        "check",
        tmp_path,
        env_extra=env_extra,
        timeout=timeout,
        require_ascii=require_ascii,
    )


def run_bump_in_subprocess(tmp_path, timeout=10, env_extra=None, require_ascii=False):
    """Run the real bump script in a child process with a fake `uv` on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 0\n")
    fake_uv.chmod(0o755)
    return _run_script_in_subprocess(
        ROOT / "scripts/bump_version.py",
        "bump_version",
        tmp_path,
        path_extra=str(bin_dir) + os.pathsep,
        env_extra=env_extra,
        timeout=timeout,
        require_ascii=require_ascii,
    )
