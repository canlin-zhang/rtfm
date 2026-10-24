# tests/test_tools.py
"""The MCP registration surface: exactly the six public tools, by name."""
import asyncio
import subprocess
import sys

from rtfm import tools  # noqa: F401 — importing registers the tools
from rtfm.app import mcp


def test_registers_exactly_the_six_public_tools():
    tools_list = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools_list} == {
        "search", "read", "reindex", "find_duplicates", "list_sources", "health_check",
    }


def test_registered_tools_keep_their_docstrings():
    by_name = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert "Search the corpus" in by_name["search"].description
    assert "Read a page range" in by_name["read"].description


def test_importing_the_facade_registers_the_tools():
    """The console entry imports the facade, not rtfm.tools — that import must register."""
    code = ("import asyncio, rtfm_server; "
            "print(sorted(t.name for t in asyncio.run(rtfm_server.mcp.list_tools())))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    expected = "['find_duplicates', 'health_check', 'list_sources', 'read', 'reindex', 'search']"
    assert out.stdout.strip() == expected
