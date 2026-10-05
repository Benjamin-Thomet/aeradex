"""Sending reports by e-mail (SMTP). Settings in allkvitt.yaml, the password never in the book:

    mail:
      smtp: mail.example.ch
      port: 587                 # 465 = SSL, otherwise STARTTLS
      benutzer: buchhaltung@example.ch
      von: buchhaltung@example.ch
      an: [chefin@example.ch, treuhand@example.ch]

    export ALLKVITT_SMTP_PASSWORD=…
"""
from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .book import Book, BookError


def config(book: Book) -> dict:
    raw = book.settings.get("mail") or {}
    to = raw.get("an") or []
    return {"smtp": str(raw.get("smtp") or ""), "port": int(raw.get("port") or 587),
            "benutzer": str(raw.get("benutzer") or raw.get("von") or ""), "von": str(raw.get("von") or ""),
            "an": [to] if isinstance(to, str) else [str(x) for x in to]}


def send(book: Book, subject: str, body: str, attachments: list[Path], an: list[str] | None = None) -> str:
    cfg = config(book)
    recipients = an or cfg["an"]
    if not cfg["smtp"] or not cfg["von"] or not recipients:
        raise BookError("E-Mail ist nicht eingerichtet: allkvitt.yaml → mail: {smtp, port, von, an}")
    password = os.environ.get("ALLKVITT_SMTP_PASSWORD")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["von"], ", ".join(recipients)
    msg.set_content(body)
    for p in attachments:
        kind = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        main, sub = kind.split("/", 1)
        msg.add_attachment(Path(p).read_bytes(), maintype=main, subtype=sub, filename=p.name)
    context = ssl.create_default_context()
    try:
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["smtp"], cfg["port"], context=context, timeout=30)
        else:
            server = smtplib.SMTP(cfg["smtp"], cfg["port"], timeout=30)
            server.starttls(context=context)
        with server:
            if password:
                server.login(cfg["benutzer"], password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise BookError(f"E-Mail konnte nicht gesendet werden: {exc}") from None
    return ", ".join(recipients)
