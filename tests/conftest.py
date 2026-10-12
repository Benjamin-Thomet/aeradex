"""Shared test setup: no test reads or writes the real ~/.config/aeradex (keys, users)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("AERADEX_CONFIG", str(tmp_path_factory.mktemp("config")))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
