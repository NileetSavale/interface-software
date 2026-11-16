"""The green-screen surface, end to end with a scripted planner in place of Claude: discover on the terminal host,
record an artifact, replay it with no model through faults, on a second bank, and against the commit guard."""
import asyncio
import json
from pathlib import Path

import pytest

from apps.mock_terminal.app import TerminalBank
from interface.artifact.io import load_artifact
from interface.artifact.schema import Policy
from interface.discovery.loop import discover
from interface.discovery.planner import ScriptedPlanner
from interface.evidence import RUNS, Audit
from interface.replay.executor import Replayer
from interface.replay.result import BusinessOutcome, Failed, Success
from interface.surface.terminal import TerminalSurface
from tests.conftest import ENV, free_port
from tests.test_end_to_end import call, ref

ROOT = Path(__file__).resolve().parent.parent
GOAL = {k: v for k, v in json.loads((ROOT / "goals" / "terminal_lookup_member_balance.json").read_text()).items()
        if k != "probes"}
SCRIPT = [
    ("type_text", call(ref=ref(r'textbox "User ID"'), text="{{secrets.APP_USER}}", intent="enter operator user id")),
    ("type_text", call(ref=ref(r'textbox "Password"'), text="{{secrets.APP_PASSWORD}}", intent="enter the password")),
    ("click", call(ref=ref(r'button "Sign on"'), intent="sign on")),
    ("type_text", call(ref=ref(r'textbox "Option"'), text="1", intent="choose member inquiry")),
    ("click", call(ref=ref(r'button "Select"'), intent="open member inquiry")),
    ("type_text", call(ref=ref(r'textbox "Member #"'), text="{{inputs.memberId}}", intent="enter the member number")),
    ("click", call(ref=ref(r'button "Search"'), intent="submit the member search")),
    ("verify_identity", call(ref=ref(r'text "Member #:"[^\n]*\n\s*- text'), input="memberId",
                             intent="confirm the member number")),
    ("extract", call(ref=ref(r'text "Name:"[^\n]*\n\s*- text'), output="memberName", type="string",
                     intent="read member name")),
    ("extract", call(ref=ref(r'text "Savings balance:"[^\n]*\n\s*- text'), output="savingsBalance", type="money",
                     intent="read savings balance")),
    ("done", call(success_ref=ref(r'heading "MEMBER DETAIL"'), summary="read name and balance")),
]


@pytest.fixture(scope="module")
def host():
    port = free_port()
    bank = TerminalBank().start_in_thread(port)
    yield bank, f"telnet://127.0.0.1:{port}"
    bank.loop.call_soon_threadsafe(bank.server.close)


@pytest.fixture(scope="module")
def discovered(host, tmp_path_factory):
    _, base = host
    planner = ScriptedPlanner(SCRIPT)
    out_dir = tmp_path_factory.mktemp("artifacts")
    outcome = asyncio.run(discover(GOAL, base, {"memberId": "12345"}, planner=planner, out_dir=out_dir, env=ENV))
    assert outcome["status"] == "recorded", outcome
    return planner, outcome, load_artifact(outcome["artifact"])


@pytest.fixture
def replay(host, discovered, tmp_path):
    bank, base = host
    approved = discovered[2].model_copy(update={"status": "approved"})

    def run(member: str, fault: str | None = None, variant: str = "a", **kw):
        bank.fault(None)
        bank.variant(variant)
        if fault:
            bank.fault(fault)
        try:
            return asyncio.run(Replayer(approved, base, {"memberId": member}, env=ENV, audit=Audit(tmp_path / "a.jsonl"),
                                        pending_dir=tmp_path, **kw).run())
        finally:
            bank.fault(None)
            bank.variant("a")
    return run


def test_discovery_on_a_green_screen_records_the_same_kind_of_artifact(discovered):
    planner, outcome, artifact = discovered
    assert artifact.app.surface == "terminal"
    text = Path(outcome["artifact"]).read_text()
    assert "12345" not in text and "teller01" not in text and "demo-only-password" not in text
    # Sign-on became the subflow run as a precondition, as on the web
    assert artifact.preconditions[0].use == "sign_on"
    # Every target is scoped to its screen code, and the last resort is the field's place on the grid
    member = next(s for s in artifact.steps if s.value == "{{inputs.memberId}}")
    assert member.target.frame == ["CB0300"]
    assert [s.kind for s in member.target.strategies] == ["role", "label", "anchor", "bbox"]
    # The model saw placeholders, never the record (§6)
    for raw in ("12345", "teller01", "Jane Doe", "4,210.55", "812.07", "04/01/1980"):
        assert not any(raw in seen for seen in planner.seen), raw
    assert any("⟨NAME_1⟩" in seen for seen in planner.seen)
    log = (RUNS / outcome["run_id"] / "events.jsonl").read_text()
    for value in ("12345", "teller01", "demo-only-password", "Jane Doe", "4,210.55"):
        assert value not in log, value


def test_replay_another_member_with_no_model(replay):
    result = replay("34567")
    assert isinstance(result, Success), result
    assert result.outputs == {"memberName": "Mei Chen", "savingsBalance": "2500.00"}
    assert not result.drift and not result.recovered


def test_the_error_taxonomy_holds_on_a_terminal(replay):
    assert isinstance(r := replay("45678"), BusinessOutcome) and r.code == "PERMISSION_DENIED", r
    recovered = replay("23456", fault="session_timeout")
    assert isinstance(recovered, Success) and [e.code for e in recovered.recovered] == ["SESSION_EXPIRED"], recovered
    assert isinstance(r := replay("23456", fault="server_error"), Failed) and r.outcome_code == "APP_ERROR", r
    assert isinstance(r := replay("23456", fault="wrong_record"), Failed) and r.error_class == "WRONG_RECORD", r
    # Values drawn blank and filled in later: waited for, never read empty
    late = replay("23456", fault="late_fill")
    assert isinstance(late, Success) and late.outputs["memberName"] == "Rafael Ortiz", late


def test_a_second_bank_relabels_and_the_grid_position_still_finds_it(replay):
    result = replay("34567", variant="b", tenant="b")
    assert isinstance(result, Success), result
    drifted = {d.step_id: d.strategy for d in result.drift}
    assert drifted == {"enter_member_number": "bbox", "submit_member_search": "bbox"}, drifted


def test_a_commit_key_is_never_sent_whatever_it_is_called(host):
    bank, base = host

    async def go():
        surface = TerminalSurface(policy=Policy(allowed_origins=[base], commit_paths=["/CB0410"]))
        await surface.navigate(base + "/")
        await surface.observe()
        for name, value in (("User ID", "teller01"), ("Password", "demo-only-password")):
            await surface.act_on_ref(next(r for r in surface._refs if surface.ref_line(r) == ("textbox", name)),
                                     "type", value)

        async def key(name):
            await surface.observe()
            await surface.act_on_ref(next(r for r in surface._refs if surface.ref_line(r) == ("button", name)), "click")

        async def field(name, value):
            await surface.observe()
            await surface.act_on_ref(next(r for r in surface._refs if surface.ref_line(r) == ("textbox", name)),
                                     "type", value)
        await key("Sign on")
        await field("Option", "2")
        await key("Select")
        for name, value in (("Member #", "12345"), ("Account type", "Savings"), ("Nickname", "Trip"),
                            ("Initial deposit", "100.00")):
            await field(name, value)
        await key("Review")
        assert (await surface.observe()).url.endswith("/CB0410")
        await key("Post")  # F10: dropped below the screen
        blocked = list(surface.blocked)
        still = (await surface.observe()).url
        await key("Cancel")  # an exit key always gets through
        back = (await surface.observe()).url
        await surface.close()
        return blocked, still, back

    before = bank.confirmed
    blocked, still, back = asyncio.run(go())
    assert blocked and "F10=Post" in blocked[0] and still.endswith("/CB0410") and back.endswith("/CB0400")
    assert bank.confirmed == before


def test_a_probe_the_model_cant_answer_is_skipped_and_the_recording_stands(host, tmp_path):
    import anthropic
    import httpx

    class Unreachable(ScriptedPlanner):
        async def turn(self, system, tools, messages):
            if len(messages) == 1 and "Goal:" not in str(messages[0]["content"]):  # a probe's question
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
            return await super().turn(system, tools, messages)

    _, base = host
    goal = {**GOAL, "probes": [{"inputs": {"memberId": "99999"}, "why": "a member number that doesn't exist",
                                "expect": "MEMBER_NOT_FOUND"}]}
    outcome = asyncio.run(discover(goal, base, {"memberId": "12345"}, planner=Unreachable(SCRIPT), out_dir=tmp_path,
                                   env=ENV))
    assert outcome["status"] == "recorded", outcome
    assert outcome["probes"] == [{"why": "a member number that doesn't exist",
                                  "result": "skipped: the model call failed (APIConnectionError)"}]
    assert Path(outcome["artifact"]).exists()


def test_a_replay_that_crashes_ends_typed_and_the_log_says_what_and_where(replay, monkeypatch):
    acted = TerminalSurface.act

    async def act(self, *a, **kw):
        if getattr(self, "_acts", 0) == 2:  # the third action, past sign-on's first two fields
            raise RuntimeError("socket went away while member 34567 was on screen")
        self._acts = getattr(self, "_acts", 0) + 1
        return await acted(self, *a, **kw)
    monkeypatch.setattr(TerminalSurface, "act", act)
    result = replay("34567")
    assert isinstance(result, Failed) and result.error_class == "INTERNAL_ERROR", result
    run = RUNS / result.run_id
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    crashed = next(e for e in events if e["event"] == "crashed")
    # The step it crashed in, and the last thing logged before it
    assert crashed["step"] == result.at_step == "sign" and crashed["after"]["step"] == "enter_password"
    assert "RuntimeError" in crashed["error"]
    assert events[-1]["event"] == "replay_finished"
    trace = (run / "traceback.txt").read_text()
    assert "RuntimeError" in trace and "act" in trace
    assert "34567" not in trace and "34567" not in result.observed  # redacted like everything else


def test_a_discovery_that_crashes_returns_and_keeps_what_support_needs(host, tmp_path):
    import anthropic
    import httpx

    class Overloaded(ScriptedPlanner):
        async def turn(self, system, tools, messages):
            if len(messages) > 4:
                response = httpx.Response(529, request=httpx.Request("POST", "https://api.anthropic.com"),
                                          headers={"request-id": "req_test123"})
                raise anthropic.APIStatusError("Overloaded", response=response, body=None)
            return await super().turn(system, tools, messages)

    _, base = host
    outcome = asyncio.run(discover(GOAL, base, {"memberId": "12345"}, planner=Overloaded(SCRIPT), out_dir=tmp_path,
                                   env=ENV))
    assert outcome["status"] == "crashed" and "APIStatusError" in outcome["error"], outcome
    events = [json.loads(line) for line in (RUNS / outcome["run_id"] / "events.jsonl").read_text().splitlines()]
    crashed = next(e for e in events if e["event"] == "crashed")
    assert crashed["status_code"] == 529 and crashed["request_id"] == "req_test123"
    assert events[-1]["event"] == "discovery_finished" and events[-1]["status"] == "crashed"


def test_the_host_logs_screens_keys_and_faults_never_what_was_typed(replay):
    log = Path("runs", "mock-terminal.log")
    before = log.read_text().count("\n") if log.exists() else 0
    replay("34567", fault="wrong_record")
    lines = log.read_text().splitlines()[before:]
    events = [json.loads(line) for line in lines]
    kinds = {e["event"] for e in events}
    assert {"connected", "screen", "key", "fault_fired", "disconnected"} <= kinds, kinds
    assert any(e["event"] == "key" and e["screen"] == "CB0300" and e["key"] == "Enter" for e in events)
    text = "\n".join(lines)
    for typed in ("34567", "teller01", "demo-only-password"):
        assert typed not in text, typed
