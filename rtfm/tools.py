"""Explicit MCP tool registration.

The six public functions live in `rtfm.query`; this module is the one place that
says which of them the server exposes.
"""

from __future__ import annotations

from . import query
from .app import mcp

for fn in (query.search, query.read, query.reindex, query.find_duplicates,
           query.list_sources, query.health_check):
    mcp.add_tool(fn)
