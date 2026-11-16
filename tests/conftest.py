import socket
import threading
import time

import pytest
import uvicorn

OPERATOR = {"APP_USER": "teller01", "APP_PASSWORD": "demo-only-password"}
# Each bank's own copy, as a tenant's replay looks them up (D26); the mock app has one demo operator for all three
ENV = {**OPERATOR, **{f"{k}__{bank}": v for bank in "ABC" for k, v in OPERATOR.items()}}


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
