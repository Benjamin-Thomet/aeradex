"""MCP server: the aeradex tools for Claude Code, opencode, agy …

    claude mcp add aeradex -- aeradex --buch /pfad/zum/buch mcp

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
    """The tools of aeradex plus those of the plugins the book enables."""
    server = MCPServer("aeradex", instructions=tools.instructions_for())
    # AERADEX_MCP_TOOLS: only these tools (comma-separated), e.g. for an agent that may only read and draft.
    only = {n.strip() for n in os.environ.get("AERADEX_MCP_TOOLS", "").split(",") if n.strip()}
    for fn in tools.shared_for():
        if not only or fn.__name__ in only:
            server.tool()(fn)
    return server


def serve() -> None:
    from . import gitlog
    os.environ.setdefault("AERADEX_AUTHOR", gitlog.AGENT_AUTHOR)
    build_server().run("stdio")
