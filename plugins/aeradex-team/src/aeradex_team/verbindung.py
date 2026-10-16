"""Connection to the portal: where it is, the sync token, and a small JSON client.

The token never goes into the book: it lives in ~/.config/aeradex/team.yaml (mode 600) or in the environment
(AERADEX_TEAM_URL, AERADEX_TEAM_TOKEN). Local state (last sync, errors, invites not yet delivered) lives in
.aeradex/lokal/team.yaml next to the book, outside git.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml
from aeradex.book import Book, BookError
from aeradex.storage import LOCAL


def config_path() -> Path:
    base = os.environ.get("AERADEX_CONFIG") or Path.home() / ".config" / "aeradex"
    return Path(base) / "team.yaml"


def config() -> dict:
    data = {}
    path = config_path()
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    url = (os.environ.get("AERADEX_TEAM_URL") or data.get("url") or "").rstrip("/")
    token = os.environ.get("AERADEX_TEAM_TOKEN") or data.get("token") or ""
    return {"url": url, "token": token}


def save_config(url: str, token: str) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"url": url.rstrip("/"), "token": token}), encoding="utf-8")
    path.chmod(0o600)
    return path


def state_path(book: Book) -> Path:
    return book.root / LOCAL / "team.yaml"


def state(book: Book) -> dict:
    path = state_path(book)
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def save_state(book: Book, data: dict) -> None:
    path = state_path(book)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class PortalError(Exception):
    """The portal could not be reached or refused a call."""


Transport = Callable[[str, str, Any], Any]


def http_transport(url: str, token: str, timeout: float = 30) -> Transport:
    def send(method: str, path: str, body: Any = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{url}/app/api/sync{path}", data=data, method=method,
                                     headers={"authorization": f"Bearer {token}", "content-type": "application/json",
                                              "user-agent": "aeradex-team"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("fehler") or exc.reason
            except ValueError:
                message = exc.reason
            raise PortalError(f"Portal: {message} ({exc.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PortalError(f"Portal nicht erreichbar: {getattr(exc, 'reason', exc)}") from None
    return send


def mail_domain() -> str:
    """For the git author of entries from the app: «Name (App) <m0001@app.<portal host>>»."""
    host = urllib.parse.urlsplit(config()["url"] or "").hostname or "localhost"
    return "app." + host.removeprefix("www.")


def transport() -> Transport:
    cfg = config()
    if not cfg["url"] or not cfg["token"]:
        raise BookError(f"Portal nicht eingerichtet: `aeradex team verbinden --url https://… --token …` "
                        f"(wird in {config_path()} gespeichert)")
    if not cfg["url"].startswith("https://") and not cfg["url"].startswith("http://127.0.0.1"):
        raise BookError("Das Portal muss über https erreichbar sein")
    return http_transport(cfg["url"], cfg["token"])
