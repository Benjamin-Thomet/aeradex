"""The call page as a channel: talk to the agent in the browser, without Telegram.

Everything the agent wants to show (cards to confirm, PDFs, what it is doing, the
summary after the call) goes into one feed; the call page follows it over SSE.
The page is opened locally with an access key in the link, like `aeradex ui`.
"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path


class Feed:
    """An append-only, thread-safe event list the call page follows (single person, local)."""

    MAX = 500

    def __init__(self):
        self.events: list[dict] = []
        self.offset = 0                      # number of events dropped from the front
        self.cond = threading.Condition()
        self.files: dict[str, Path] = {}     # id → document the page may open

    def push(self, event: dict) -> None:
        with self.cond:
            event = {**event, "n": self.offset + len(self.events), "zeit": time.strftime("%H:%M")}
            self.events.append(event)
            if len(self.events) > self.MAX:
                drop = len(self.events) - self.MAX
                self.events, self.offset = self.events[drop:], self.offset + drop
            self.cond.notify_all()

    def since(self, n: int, timeout: float = 15.0) -> list[dict]:
        """Events with number >= n; waits up to `timeout` for the first new one."""
        with self.cond:
            if self.offset + len(self.events) <= n:
                self.cond.wait(timeout)
            return self.events[max(0, n - self.offset):]

    def add_file(self, path: Path) -> str:
        file_id = secrets.token_hex(8)
        with self.cond:
            self.files[file_id] = Path(path)
        return file_id


class PageChannel:
    """actions.Channel for the call page."""

    def __init__(self, feed: Feed):
        self.feed = feed

    def send_text(self, text: str) -> None:
        self.feed.push({"type": "text", "text": text})

    def send_document(self, path: Path, caption: str = "") -> None:
        path = Path(path)
        self.feed.push({"type": "datei", "id": self.feed.add_file(path), "name": path.name, "text": caption})

    def send_card(self, text: str, buttons: list[tuple[str, str]]) -> None:
        self.feed.push({"type": "karte", "text": text, "knoepfe": [{"label": label, "data": data} for label, data in buttons]})
