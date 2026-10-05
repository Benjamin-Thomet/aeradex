"""MCP server: the allkvitt tools for Claude Code, opencode, agy …

    claude mcp add allkvitt -- allkvitt --buch /pfad/zum/buch mcp

The tools themselves live in `tools.py`, shared with the chat panel of the UI,
so both offer the same operations under the same rules. Commits made through
this server are attributed to the agent in the book's git history.
"""
from __future__ import annotations

import os

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

from . import tools

def build_server():
    """The tools of allkvitt plus those of the plugins the book enables."""
    server = MCPServer("allkvitt", instructions=tools.instructions_for())
    for fn in tools.shared_for():
        server.tool()(fn)
    return server


def serve() -> None:
    from . import gitlog
    os.environ.setdefault("ALLKVITT_AUTHOR", gitlog.AGENT_AUTHOR)
    build_server().run("stdio")
