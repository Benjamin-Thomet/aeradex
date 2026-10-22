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


def read_inbox_file(datei: str):
    """Liest eine Datei aus inbox/ oder belege/ des Buchs auf dem Server (Bild, PDF, Word oder Text), damit du
    den Inhalt siehst — Dateien auf deinem eigenen PC liest du direkt.

    Args:
        datei: Pfad relativ zum Buch, z.B. "inbox/quittung.pdf".
    """
    try:  # mcp >= 2
        from mcp.server.mcpserver import Image
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import Image
    from . import erfassung
    try:
        b = tools.book()
        path = tools._source_file(b, datei)
    except (tools.BookError, ValueError) as exc:
        return f"Fehler: {exc}"
    suffix = path.suffix.lower()
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        if path.stat().st_size > 20 * 1024 * 1024:
            return f"Fehler: {datei} ist grösser als 20 MB"
        return Image(data=path.read_bytes(), format="jpeg" if suffix == ".jpg" else suffix[1:])
    if suffix == ".pdf":
        # the text layer, or OCR on the server: the same readers as the Belegeingang
        _, text, notes = erfassung.analyse(b, path)
        return text[:40000] if text.strip() else "Fehler: kein Text erkannt (" + "; ".join(notes) + ")"
    return tools.read_inbox_file(datei)


def build_server():
    """The tools of aeradex plus those of the plugins the book enables."""
    server = MCPServer("aeradex", instructions=tools.instructions_for())
    # AERADEX_MCP_TOOLS: only these tools (comma-separated), e.g. for an agent that may only read and draft.
    only = {n.strip() for n in os.environ.get("AERADEX_MCP_TOOLS", "").split(",") if n.strip()}
    for fn in tools.shared_for() + [read_inbox_file]:
        if not only or fn.__name__ in only:
            server.tool()(fn)
    return server


def serve() -> None:
    from . import gitlog
    os.environ.setdefault("AERADEX_AUTHOR", gitlog.AGENT_AUTHOR)
    build_server().run("stdio")
