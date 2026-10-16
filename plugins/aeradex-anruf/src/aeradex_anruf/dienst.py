"""Where the running service (`aeradex anruf serve` / `aeradex telegram serve`) tells the aeradex UI about itself.

`.aeradex/lokal/anruf/dienst.json` (git-ignored, chmod 600): process id, start time, the call page link with its
access key, whether Telegram runs. The Nora page in aeradex reads it to show the status and the «Anrufen» button.
"""
from __future__ import annotations

import atexit
import json
import os
import time
from pathlib import Path


def path(root: Path) -> Path:
    p = Path(root) / ".aeradex" / "lokal" / "anruf" / "dienst.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def announce(root: Path, link: str, telegram: bool, port: int) -> None:
    data = {"pid": os.getpid(), "seit": time.time(), "anrufseite": link, "telegram": telegram, "port": port}
    p = path(root)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    tmp.replace(p)
    atexit.register(withdraw, root, os.getpid())


def withdraw(root: Path, pid: int | None = None) -> None:
    p = path(root)
    try:
        if pid is None or json.loads(p.read_text(encoding="utf-8")).get("pid") == pid:
            p.unlink()
    except (FileNotFoundError, ValueError):
        pass


def status(root: Path) -> dict | None:
    """The announcement, if that process still runs."""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
        os.kill(int(data["pid"]), 0)
    except (FileNotFoundError, ValueError, KeyError, ProcessLookupError, PermissionError, TypeError):
        return None
    return data
