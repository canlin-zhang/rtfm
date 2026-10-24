# tests/test_front.py
"""Contract tests for the harness fronts and their shared bootstrap.

The bootstrap is a script in each front, not an installed module, so the tests
load it with SourceFileLoader. Both fronts copy the same file byte-for-byte;
one test enforces that so the two install paths cannot drift.
"""

import importlib.machinery
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bootstrap = _load("front_bootstrap", ROOT / "plugins/claude/bootstrap.py")


def _pyproject_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_read_version_prefers_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"version": "2.0.0"}')
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"version": "1.2.3"}')
    assert bootstrap.read_version(tmp_path) == "2.0.0"


def test_read_version_falls_back_to_claude_manifest(tmp_path):
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text('{"version": "1.2.3"}')
    assert bootstrap.read_version(tmp_path) == "1.2.3"


def test_data_dir_prefers_claude_plugin_data(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert bootstrap.data_dir("claude") == tmp_path / "data"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    monkeypatch.setenv("RTFM_HOME", str(tmp_path / "home"))
    assert bootstrap.data_dir("claude") == tmp_path / "home" / "fronts" / "claude"
    assert bootstrap.data_dir("opencode") == tmp_path / "home" / "fronts" / "opencode"


def test_resolve_command_prefers_uvx(monkeypatch):
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: "/bin/uvx" if name == "uvx" else None
    )
    assert bootstrap.resolve_command("0.8.0", "claude") == [
        "uvx", "--from", "rtfm-mcp==0.8.0", "rtfm-server"
    ]


def test_resolve_command_falls_back_to_uv(monkeypatch):
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None
    )
    assert bootstrap.resolve_command("0.8.0", "claude") == [
        "uv", "tool", "run", "--from", "rtfm-mcp==0.8.0", "rtfm-server"
    ]


def test_resolve_command_uses_venv_when_no_uv(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        bootstrap,
        "venv_server",
        lambda version, front: ["/v/bin/python", "-m", "rtfm_server"],
    )
    assert bootstrap.resolve_command("0.8.0", "claude") == [
        "/v/bin/python", "-m", "rtfm_server"
    ]


def test_main_reports_fatal_to_stderr(monkeypatch, capsys):
    def boom(root):
        raise RuntimeError("boom")

    monkeypatch.setattr(bootstrap, "read_version", boom)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(["--front", "claude"])
    assert exc.value.code == 1
    assert "FATAL" in capsys.readouterr().err


def test_venv_install_keeps_stdout_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "data_dir", lambda front: tmp_path)

    def fake_create(path, with_pip):
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "bin" / "python").write_text("")

    monkeypatch.setattr(bootstrap.venv, "create", fake_create)
    calls = []
    monkeypatch.setattr(
        bootstrap.subprocess, "check_call", lambda cmd, **kwargs: calls.append(kwargs)
    )

    command = bootstrap.venv_server("0.8.0", "claude")

    assert command == [str(tmp_path / "venv" / "bin" / "python"), "-m", "rtfm_server"]
    assert calls and calls[0]["stdout"] is sys.stderr
