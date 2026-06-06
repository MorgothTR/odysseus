"""Regression tests for the ChromaDB singleton client.

Covers native embedded ChromaDB defaults, explicit HTTP-service mode, the
fast-fail HTTP preflight, and the rule that failed setup must not poison the
cached singleton.
"""

import socket
import sys
import time
import types

import pytest

import src.chroma_client as cc


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.heartbeat_called = False

    def heartbeat(self):
        self.heartbeat_called = True


class _FakeChroma(types.SimpleNamespace):
    def __init__(self, persistent=True, http=True):
        super().__init__()
        self.persistent_calls = []
        self.http_calls = []
        if persistent:
            self.PersistentClient = self._persistent
        if http:
            self.HttpClient = self._http

    def _persistent(self, **kwargs):
        client = _FakeClient(**kwargs)
        self.persistent_calls.append(client)
        return client

    def _http(self, **kwargs):
        client = _FakeClient(**kwargs)
        self.http_calls.append(client)
        return client


@pytest.fixture(autouse=True)
def _reset_chroma(monkeypatch):
    cc.reset_client()
    monkeypatch.delenv("CHROMADB_HOST", raising=False)
    monkeypatch.delenv("CHROMADB_PORT", raising=False)
    monkeypatch.setattr(cc, "_settings", lambda: None)
    yield
    cc.reset_client()


def _install_fake_chroma(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "chromadb", fake)
    return fake


def _free_port() -> int:
    """Bind to port 0, grab the assigned port, release it; nothing listens."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_port_open_false_for_closed_port_and_is_fast():
    port = _free_port()
    t0 = time.monotonic()
    assert cc._port_open("127.0.0.1", port, timeout=1.0) is False
    # The whole point: we fail fast, nowhere near the 30-60s OS timeout.
    assert time.monotonic() - t0 < 5.0


def test_port_open_true_for_listening_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    host, port = srv.getsockname()
    try:
        assert cc._port_open(host, port, timeout=1.0) is True
    finally:
        srv.close()


def test_default_uses_embedded_persistent_client(monkeypatch, tmp_path):
    fake = _install_fake_chroma(monkeypatch, _FakeChroma())
    persist = tmp_path / "chroma"
    monkeypatch.setattr(cc, "_persistent_path", lambda: str(persist))

    client = cc.get_chroma_client()

    assert client is fake.persistent_calls[0]
    assert client.kwargs == {"path": str(persist)}
    assert client.heartbeat_called is True
    assert persist.is_dir()
    assert fake.http_calls == []
    assert cc.get_chroma_client() is client


def test_external_env_uses_http_client(monkeypatch):
    fake = _install_fake_chroma(monkeypatch, _FakeChroma())
    monkeypatch.setenv("CHROMADB_HOST", "chromadb")
    monkeypatch.setenv("CHROMADB_PORT", "8000")
    monkeypatch.setattr(cc, "_port_open", lambda host, port: True)

    client = cc.get_chroma_client()

    assert client is fake.http_calls[0]
    assert client.kwargs == {"host": "chromadb", "port": 8000}
    assert client.heartbeat_called is True
    assert fake.persistent_calls == []


def test_http_client_does_not_cache_when_unreachable(monkeypatch):
    fake = _install_fake_chroma(monkeypatch, _FakeChroma())
    monkeypatch.setenv("CHROMADB_HOST", "127.0.0.1")
    monkeypatch.setenv("CHROMADB_PORT", str(_free_port()))
    monkeypatch.setattr(cc, "_port_open", lambda host, port: False)

    with pytest.raises(RuntimeError, match="not reachable"):
        cc.get_chroma_client()

    assert cc._client is None
    assert fake.http_calls == []


def test_embedded_mode_rejects_http_only_chromadb_client(monkeypatch):
    _install_fake_chroma(monkeypatch, _FakeChroma(persistent=False, http=True))

    with pytest.raises(RuntimeError, match="Embedded ChromaDB is not available"):
        cc.get_chroma_client()

    assert cc._client is None
