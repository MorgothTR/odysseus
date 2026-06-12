"""
chroma_client.py

Singleton ChromaDB client.

Native/manual installs use embedded persistent ChromaDB under data/chroma by
default. Docker and advanced users can opt into a standalone HTTP ChromaDB
service by setting CHROMADB_HOST or CHROMADB_PORT.
"""

import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

_client = None

# A short connect probe so an unreachable HTTP ChromaDB fails fast instead of
# blocking on the OS connection timeout. Tunable via CHROMADB_CONNECT_TIMEOUT.
_CONNECT_TIMEOUT = float(os.getenv("CHROMADB_CONNECT_TIMEOUT", "2.0"))


def _port_open(host: str, port: int, timeout: float = None) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout or _CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def _use_http_chroma() -> bool:
    """Return True when env explicitly selects a standalone Chroma service."""
    return "CHROMADB_HOST" in os.environ or "CHROMADB_PORT" in os.environ


def _persistent_path() -> str:
    from src.constants import DATA_DIR

    return str(Path(DATA_DIR) / "chroma")


def _settings():
    try:
        from chromadb.config import Settings

        return Settings(anonymized_telemetry=False)
    except Exception:
        return None


def _build_http_client(chromadb):
    host = os.getenv("CHROMADB_HOST", "localhost")
    port = int(os.getenv("CHROMADB_PORT", "8100"))

    if not _port_open(host, port):
        raise RuntimeError(
            f"ChromaDB is not reachable at {host}:{port}. Start the ChromaDB "
            "service or unset CHROMADB_HOST / CHROMADB_PORT to use embedded "
            "native ChromaDB storage."
        )

    kwargs = {"host": host, "port": port}
    settings = _settings()
    if settings is not None:
        kwargs["settings"] = settings
    client = chromadb.HttpClient(**kwargs)
    client.heartbeat()
    logger.info("ChromaDB HTTP client connected: %s:%s", host, port)
    return client


def _build_persistent_client(chromadb):
    if not hasattr(chromadb, "PersistentClient"):
        raise RuntimeError(
            "Embedded ChromaDB is not available. Remove the HTTP-only "
            "chromadb-client package and install the full package with: "
            "pip uninstall chromadb-client -y && pip install --force-reinstall chromadb"
        )

    path = _persistent_path()
    os.makedirs(path, exist_ok=True)
    kwargs = {"path": path}
    settings = _settings()
    if settings is not None:
        kwargs["settings"] = settings
    client = chromadb.PersistentClient(**kwargs)
    client.heartbeat()
    logger.info("ChromaDB embedded persistent client ready: %s", path)
    return client


def get_chroma_client():
    """Get or create the singleton ChromaDB client.

    Native/manual installs use embedded persistent ChromaDB unless the
    CHROMADB_HOST or CHROMADB_PORT environment variable explicitly selects an
    external HTTP service.
    """
    global _client
    if _client is not None:
        return _client

    try:
        import chromadb
    except ImportError as e:
        raise RuntimeError(
            "ChromaDB integration is not installed. Install the full package "
            "with: pip install chromadb"
        ) from e

    # Health check before caching: if the service/path is not healthy, leave
    # _client unset so the next call can retry after the problem is fixed.
    _client = _build_http_client(chromadb) if _use_http_chroma() else _build_persistent_client(chromadb)
    return _client


def reset_client():
    """Reset the singleton (e.g. after config change)."""
    global _client
    _client = None
