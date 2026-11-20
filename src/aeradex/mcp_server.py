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


PDF_FOLDERS = ("rechnungen", "mahnungen", "offerten", "belege", "inbox")


def _pdf_path(b, nummer: str, datei: str):
    """The stored PDF of an invoice number, or a book-relative .pdf in one of PDF_FOLDERS."""
    from . import api, invoices
    if nummer.strip():
        meta = invoices.invoice(b, nummer.strip())     # BookError if unknown
        path = api.meta_pdf_path(b, meta)
        if not path.is_file():
            raise ValueError(f"zu {nummer} ist keine PDF gespeichert")
        return path
    path = (b.root / (datei or "")).resolve()
    rel = path.relative_to(b.root.resolve()) if path.is_relative_to(b.root.resolve()) else None
    if rel is None or path.suffix.lower() != ".pdf" or not path.is_file() or rel.parts[:1] not in [(f,) for f in PDF_FOLDERS]:
        raise ValueError(f"{datei} ist keine PDF in {', '.join(f + '/' for f in PDF_FOLDERS)}")
    return path


def document_pdf(nummer: str = "", datei: str = ""):
    """Gibt eine PDF aus dem Buch auf dem Server unverändert als Datei heraus — z.B. die ausgestellte Rechnung
    (mit QR-Zahlteil), um sie einem Mail-Entwurf anzuhängen. Der Client legt sie als lokale Datei ab und nennt den Pfad.

    Args:
        nummer: Rechnungsnummer, z.B. "INV-0252" (die PDF, die beim Ausstellen erzeugt wurde).
        datei: statt einer Nummer ein Pfad relativ zum Buch, z.B. "mahnungen/INV-0250-M1.pdf" oder
            "offerten/2026/OF-0003.pdf" (nur rechnungen/, mahnungen/, offerten/, belege/, inbox/).
    """
    import base64
    from mcp.types import BlobResourceContents, EmbeddedResource, TextContent
    try:
        b = tools.book()
        path = _pdf_path(b, nummer, datei)
    except (tools.BookError, ValueError) as exc:
        return f"Fehler: {exc}"
    data = path.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        return f"Fehler: {b.rel(path)} ist grösser als 20 MB"
    rel = b.rel(path)
    return [EmbeddedResource(type="resource", resource=BlobResourceContents(
                uri=f"aeradex://buch/{rel}", mimeType="application/pdf", blob=base64.b64encode(data).decode())),
            TextContent(type="text", text=f"{rel} ({len(data)} Bytes), Dateiname {path.name}")]


def build_server():
    """The tools of aeradex plus those of the plugins the book enables."""
    server = MCPServer("aeradex", instructions=tools.instructions_for())
    # AERADEX_MCP_TOOLS: only these tools (comma-separated), e.g. for an agent that may only read and draft.
    only = {n.strip() for n in os.environ.get("AERADEX_MCP_TOOLS", "").split(",") if n.strip()}
    for fn in tools.shared_for() + [read_inbox_file, document_pdf]:
        if not only or fn.__name__ in only:
            server.tool()(fn)
    return server


def serve() -> None:
    from . import gitlog
    os.environ.setdefault("AERADEX_AUTHOR", gitlog.AGENT_AUTHOR)
    build_server().run("stdio")
