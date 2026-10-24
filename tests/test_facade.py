# tests/test_facade.py
"""The console facade: exactly the six tools, the app, and main."""
import tomllib
from pathlib import Path

import rtfm_server
from rtfm import app, query


def test_facade_exposes_the_six_public_tools():
    for name in ("search", "read", "reindex", "find_duplicates", "list_sources", "health_check"):
        assert getattr(rtfm_server, name) is getattr(query, name)


def test_facade_exposes_the_shared_app():
    assert rtfm_server.mcp is app.mcp


def test_facade_keeps_the_console_entry_point():
    scripts = tomllib.loads(Path("pyproject.toml").read_text())["project"]["scripts"]
    assert scripts["rtfm-server"] == "rtfm_server:main"
    assert callable(rtfm_server.main)


def test_facade_exposes_no_private_names():
    for name in ("_git", "_freshness", "_ingest", "_extract_rows"):
        assert not hasattr(rtfm_server, name)
