"""The whole thread with a scripted planner in place of Claude: discover on the live mock app, record an artifact,
approve it, replay it with other inputs. No API calls, so it runs on every test run."""
import asyncio
import re
import socket
import threading
import time

import pytest
import uvicorn

from interfaze.artifact.io import load_artifact
from interfaze.discovery.loop import discover
from interfaze.discovery.planner import ScriptedPlanner
from interfaze.evidence import RUNS
from interfaze.replay.executor import Replayer
from interfaze.replay.result import BusinessOutcome, Failed, InvalidInput, Success

ENV = {"APP_USER": "teller01", "APP_PASSWORD": "demo-only-password"}
GOAL = {
    "capability": "core_banking.lookup_member_balance", "title": "Look up a member's savings balance",
    "description": "Returns the member's name and savings balance.", "product": "mock-core-banking",
    "goal": "Sign on, look up member {{inputs.memberId}}, read name and savings balance.",
    "inputs": {"memberId": {"type": "string", "description": "5-digit member number", "pattern": "^[0-9]{5}$",
                            "sensitive": True, "identifies": "member"}},
    "secrets": ["APP_USER", "APP_PASSWORD"],
}


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config("apps.mock_core_banking.app:app", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def ref(pattern: str):
    """Script argument that finds a ref on the screen the planner was just shown."""
    def pick(screen: str) -> str:
        match = re.search(pattern + r"[^\n]*?\[ref=(\w+)\]", screen)
        assert match, f"{pattern} not on screen:\n{screen}"
        return match.group(1)
    return pick


def call(**args):
    return lambda screen: {k: v(screen) if callable(v) else v for k, v in args.items()}


SCRIPT = [
    ("type_text", call(ref=ref(r'textbox "User ID"'), text="{{secrets.APP_USER}}", intent="enter operator user id")),
    ("type_text", call(ref=ref(r'textbox "Password"'), text="{{secrets.APP_PASSWORD}}", intent="enter operator password")),
    ("click", call(ref=ref(r'button "Sign On"'), intent="sign on")),
    ("click", call(ref=ref(r'link "Member Inquiry"'), intent="open member inquiry")),
    ("type_text", call(ref=ref(r'textbox "Member #"'), text="{{inputs.memberId}}", intent="enter the member number")),
    ("click", call(ref=ref(r'button "Search"'), intent="submit the member search")),
    ("extract", call(ref=ref(r'cell "Name:"[^\n]*\n\s*- cell'), output="memberName", type="string", intent="read member name")),
    ("extract", call(ref=ref(r'cell "Savings"[^\n]*\n[^\n]*\n\s*- cell'), output="savingsBalance", type="money",
                     intent="read savings balance")),
    ("done", call(success_ref=ref(r'cell "Member Detail"'), summary="read name and balance")),
]


def test_discover_then_replay(base_url, tmp_path):
    # done is refused until the identifying input is verified: the schema requires it (D16)
    script = [*SCRIPT[:-1], SCRIPT[-1],
              ("verify_identity", call(ref=ref(r'cell "Member #:"[^\n]*\n\s*- cell'), input="memberId",
                                       intent="confirm the member number")),
              SCRIPT[-1]]
    planner = ScriptedPlanner(script)
    outcome = asyncio.run(discover(GOAL, base_url, {"memberId": "12345"}, planner=planner, out_dir=tmp_path, env=ENV))
    assert outcome["status"] == "recorded", outcome
    assert any("no identity checkpoint verifies it" in seen for seen in planner.seen)

    artifact = load_artifact(outcome["artifact"])
    text = (tmp_path / "core_banking.lookup_member_balance.json").read_text()
    assert "12345" not in text and "demo-only-password" not in text and "teller01" not in text
    assert any(s.value == "{{inputs.memberId}}" for s in artifact.steps)

    # The model never saw the member number or the credentials
    assert not any("12345" in seen or "teller01" in seen for seen in planner.seen)
    # The run log keeps neither those nor the values read off the screen
    log = (RUNS / outcome["run_id"] / "events.jsonl").read_text()
    for value in ("12345", "teller01", "demo-only-password", "Jane Doe", "4,210.55"):
        assert value not in log, value

    approved = artifact.model_copy(update={"status": "approved"})
    replay = lambda member: asyncio.run(Replayer(approved, base_url, {"memberId": member}, env=ENV).run())

    other = replay("34567")
    assert isinstance(other, Success), other
    assert other.outputs == {"memberName": "Mei Chen", "savingsBalance": "2500.00"}
    assert not other.drift
    replay_log = "".join(p.read_text() for p in (RUNS / other.run_id).glob("*.json*"))
    assert "Mei Chen" not in replay_log and "2500.00" not in replay_log and "34567" not in replay_log

    assert isinstance(replay("1234"), InvalidInput)
    missing = replay("99999")
    # No declared outcome yet (outcomes come from review and the library in Pass 2), so a not-found page is a hard
    # failure that says where it stopped
    assert isinstance(missing, (Failed, BusinessOutcome))
    assert missing.at_step == "read_member_name" if isinstance(missing, Failed) else True
