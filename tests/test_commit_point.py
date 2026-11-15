"""open_sub_account: a capability that changes something reaches the commit point and never presses it (D1)."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from interfaze.artifact.io import load_artifact
from interfaze.artifact.schema import Step
from interfaze.discovery.loop import discover
from interfaze.discovery.planner import ScriptedPlanner
from interfaze.evidence import Audit
from interfaze.replay.executor import Replayer
from interfaze.replay.result import Failed, Success
from tests.conftest import ENV
from tests.test_end_to_end import SCRIPT, call, ref

GOAL = {k: v for k, v in json.loads(Path("goals/open_sub_account.json").read_text()).items() if k != "probes"}
INPUTS = {"memberId": "12345", "accountType": "Holiday Club", "nickname": "Rainy Day", "initialDeposit": "250.00"}

FORM = [
    ("click", call(ref=ref(r'link "Open Sub-Account"'), intent="start a new sub-account")),
    ("select_option", call(ref=ref(r'cell "Account type"[^\n]*\n[^\n]*\n\s*- combobox'), option="{{inputs.accountType}}",
                           intent="choose the account type")),
    ("type_text", call(ref=ref(r'textbox "Nickname"'), text="{{inputs.nickname}}", intent="enter the nickname")),
    ("type_text", call(ref=ref(r'cell "Initial deposit"[^\n]*\n[^\n]*\n\s*- textbox'), text="{{inputs.initialDeposit}}",
                       intent="enter the opening deposit")),
    ("click", call(ref=ref(r'button "Continue"'), intent="continue to review")),
]


def confirmed(base_url) -> int:
    return httpx.get(base_url + "/__confirmed").json()["count"]


@pytest.fixture(scope="module")
def recorded(base_url, tmp_path_factory):
    before = confirmed(base_url)
    script = [*SCRIPT[:6], *FORM,
              # The model tries to finish the job: policy refuses, and says why
              ("click", call(ref=ref(r'button "Confirm"'), intent="open the account")),
              ("verify_identity", call(ref=ref(r'cell "Member #:"[^\n]*\n\s*- cell'), input="memberId",
                                       intent="confirm the member number")),
              ("done", call(success_ref=ref(r'cell "Account type:"'), summary="at the review screen"))]
    planner = ScriptedPlanner(script)
    outcome = asyncio.run(discover(GOAL, base_url, INPUTS, planner=planner, out_dir=tmp_path_factory.mktemp("a"),
                                   env=ENV, escalation_model=None))
    assert outcome["status"] == "recorded", outcome
    return planner, load_artifact(outcome["artifact"]), before


def test_discovery_cannot_press_confirm(base_url, recorded):
    planner, artifact, before = recorded
    assert any("blocked by policy: irreversible action (button 'Confirm')" in seen for seen in planner.seen)
    assert confirmed(base_url) == before
    assert not any(s.target and any(getattr(st, "name", None) == "Confirm" for st in s.target.strategies)
                   for s in artifact.steps)


def test_artifact_reaches_the_commit_point_with_the_right_risks(recorded):
    _, artifact, _ = recorded
    assert artifact.policy.max_risk == "reversible"
    assert {s.id: s.risk for s in artifact.steps}["continue_review"] == "reversible"
    # Unlabelled fields were found by the text in the neighbouring cell
    deposit = next(s for s in artifact.steps if s.id == "enter_opening_deposit")
    assert deposit.target.strategies[0].kind == "anchor"
    # A capability that changes something may not restart its steps after a timeout: a person decides
    expired = next(o for o in artifact.outcomes if o.code == "SESSION_EXPIRED")
    assert expired.kind == "escalate"


def test_replay_stops_at_review_and_opens_nothing(base_url, recorded, tmp_path):
    _, artifact, _ = recorded
    before = confirmed(base_url)
    approved = artifact.model_copy(update={"status": "approved"})
    other = {**INPUTS, "memberId": "34567", "accountType": "Certificate", "nickname": "College", "initialDeposit": "1000.00"}
    result = asyncio.run(Replayer(approved, base_url, other, env=ENV, audit=Audit(tmp_path / "a.jsonl")).run())
    assert isinstance(result, Success), result
    assert confirmed(base_url) == before


def test_a_locator_that_lands_on_confirm_is_refused_at_replay(base_url, recorded, tmp_path):
    """The recorded risk is checked again on the live element: a broad locator that happens to find the commit button
    (here a deliberately loose text pattern, standing in for a fallback strategy after the app changed) is blocked."""
    _, artifact, _ = recorded
    before = confirmed(base_url)
    loose = Step.model_validate({
        "id": "press_the_button", "intent": "press the review screen's button", "action": "click", "risk": "reversible",
        "target": {"frame": ["main"], "strategies": [{"kind": "text", "text": {"matches": "^(Continue|Confirm)$"}}]}})
    tampered = artifact.model_copy(update={"status": "approved", "steps": [*artifact.steps, loose]})
    result = asyncio.run(Replayer(tampered, base_url, INPUTS, env=ENV, audit=Audit(tmp_path / "a.jsonl")).run())
    assert isinstance(result, Failed) and result.error_class == "POLICY_VIOLATION", result
    assert "irreversible" in result.observed
    assert confirmed(base_url) == before
