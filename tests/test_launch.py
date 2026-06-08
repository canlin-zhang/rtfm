# tests/test_launch.py
import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("launch", str(ROOT / "bin" / "launch"))
spec = importlib.util.spec_from_loader("launch", _loader)
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


def test_parse_pep723_deps_reads_server_block():
    deps = launch.parse_pep723_deps(ROOT / "rtfm_server.py")
    assert "pymupdf" in deps and "pypdf" in deps
    assert any(d.startswith("mcp") for d in deps)
