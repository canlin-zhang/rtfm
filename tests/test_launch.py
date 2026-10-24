# tests/test_launch.py
import importlib.machinery
import importlib.util
from pathlib import Path

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
