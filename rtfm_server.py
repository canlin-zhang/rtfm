#!/usr/bin/env python3
"""rtfm — Read The Full Manual. Local document-corpus search MCP server."""
from __future__ import annotations

from rtfm import tools as _tools  # noqa: F401 — registers the tools on mcp
from rtfm.app import mcp
from rtfm.query import (
    find_duplicates,
    health_check,
    list_sources,
    read,
    reindex,
    search,
)

__all__ = ["search", "read", "reindex", "find_duplicates", "list_sources",
           "health_check", "mcp", "main"]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
