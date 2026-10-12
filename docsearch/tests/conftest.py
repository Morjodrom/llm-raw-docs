import socket
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Runtime network access is forbidden")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "документы"
    root.mkdir()
    return root


@pytest.fixture
def engine(tmp_path):
    from docsearch import DocSearch
    with DocSearch(tmp_path / "search.sqlite") as engine:
        yield engine
