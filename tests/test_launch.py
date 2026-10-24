# tests/test_launch.py
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("launch", str(ROOT / "bin" / "launch"))
spec = importlib.util.spec_from_loader("launch", _loader)
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


def test_launcher_reads_dependencies_from_pyproject():
    deps = launch.project_deps(ROOT)
    assert any(d.startswith("mcp") for d in deps)
    assert "pymupdf" in deps and "pypdf" in deps


def test_deps_stamp_is_order_independent():
    assert launch.deps_stamp(["a", "b"]) == launch.deps_stamp(["b", "a"])
    assert launch.deps_stamp(["a"]) != launch.deps_stamp(["a", "b"])


def test_run_with_venv_rebuilds_a_venv_without_pip(monkeypatch, tmp_path):
    monkeypatch.setenv("RTFM_HOME", str(tmp_path))
    vdir = tmp_path / "venv"
    (vdir / "bin").mkdir(parents=True)
    (vdir / "bin" / "python").write_text("")
    monkeypatch.setattr(launch, "_has_pip", lambda py: False)
    created = []

    def create(path, with_pip):
        created.append(path)
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "bin" / "python").write_text("")

    monkeypatch.setattr(launch.venv, "create", create)
    installed = []
    monkeypatch.setattr(
        launch.subprocess, "check_call", lambda cmd, **kw: installed.append(cmd))
    monkeypatch.setattr(launch.os, "execv", lambda file, args: None)

    launch.run_with_venv()

    assert created == [vdir]
    assert installed and installed[0][1:3] == ["-m", "pip"]


def test_run_with_venv_reuses_a_venv_with_pip(monkeypatch, tmp_path):
    monkeypatch.setenv("RTFM_HOME", str(tmp_path))
    vdir = tmp_path / "venv"
    (vdir / "bin").mkdir(parents=True)
    (vdir / "bin" / "python").write_text("")
    (vdir / ".deps").write_text(launch.deps_stamp(launch.project_deps(ROOT)))
    monkeypatch.setattr(launch, "_has_pip", lambda py: True)
    monkeypatch.setattr(launch.venv, "create", lambda *a, **k: pytest.fail("recreated"))
    monkeypatch.setattr(launch.subprocess, "check_call", lambda *a, **k: pytest.fail("installed"))
    calls = []
    monkeypatch.setattr(
        launch.os, "execv", lambda file, args: calls.append((file, args)))

    launch.run_with_venv()

    assert calls and calls[0][0] == str(vdir / "bin" / "python")
