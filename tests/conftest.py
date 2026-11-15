import socket
import threading
import time

import pytest
import uvicorn

ENV = {"APP_USER": "teller01", "APP_PASSWORD": "demo-only-password"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    """The mock core-banking app on a free port, for the whole test session."""
    server = uvicorn.Server(uvicorn.Config("apps.mock_core_banking.app:app", port=free_port(), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{server.config.port}"
    server.should_exit = True
