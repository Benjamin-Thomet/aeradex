import pytest

from aeradex_team_portal import auth
from aeradex_team_portal.app import Config, create_app
from aeradex_team_portal.testing import TOKEN, client


@pytest.fixture
def cfg(tmp_path):
    return Config(tmp_path / "data", b"x" * 40, auth.sha256(TOKEN), secure_cookie=True)


@pytest.fixture
def app(cfg):
    return create_app(cfg)


@pytest.fixture
def pc(app):
    """The book (sync token)."""
    c = client(app)
    c.headers["authorization"] = f"Bearer {TOKEN}"
    return c
