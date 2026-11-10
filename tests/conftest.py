import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from reset_db import reset  # noqa: E402

from app.bot import BotConfig, ChatBot  # noqa: E402
from app.mcp_client import MCPConnection, MCPToolbox  # noqa: E402
from app.storage import Storage  # noqa: E402
from tests.helpers import FakeClaude  # noqa: E402


@pytest.fixture
def mock_db(tmp_path, monkeypatch):
    db = tmp_path / "isp.db"
    reset(db)
    monkeypatch.setenv("MOCK_DB_PATH", str(db))
    return db


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_mock(mock_db):
    """Mock MCP server over HTTP in a subprocess; `.stop()` simulates the tracker going down."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "mock_mcp.server", "--http", "--port", str(port)],
        cwd=ROOT, env={**os.environ, "MOCK_DB_PATH": str(mock_db)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    def stop():
        proc.terminate()
        proc.wait(5)

    yield NS(url=f"http://127.0.0.1:{port}/mcp", stop=stop)
    if proc.poll() is None:
        stop()


async def _make_env(tmp_path, mock_db, target: str):
    storage = Storage(tmp_path / "app.db")
    toolbox = await MCPToolbox([MCPConnection(target)], call_timeout=5).__aenter__()

    def make(script, **cfg):
        fake = FakeClaude(script)
        return ChatBot(fake, toolbox, storage, BotConfig(**cfg)), fake

    return NS(make=make, storage=storage, mock_db=mock_db, toolbox=toolbox)


@pytest.fixture
async def env(tmp_path, mock_db):
    """Bot wired to the real mock MCP server over stdio, with a scripted fake Claude."""
    return await _make_env(tmp_path, mock_db, "python -m mock_mcp.server")


@pytest.fixture
async def http_env(tmp_path, mock_db, http_mock):
    """Same, but the mock runs over HTTP and can be stopped (`.server.stop()`)."""
    e = await _make_env(tmp_path, mock_db, http_mock.url)
    e.server = http_mock
    return e
