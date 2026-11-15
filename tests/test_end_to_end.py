"""The whole thread with a scripted planner in place of Claude: discover on the live mock app, record an artifact,
approve it, replay it with other inputs. No API calls, so it runs on every test run."""
import asyncio
import re
from pathlib import Path

import httpx
import pytest

from interfaze.artifact.io import load_artifact
from interfaze.discovery.loop import discover
from interfaze.discovery.planner import ScriptedPlanner
from interfaze.evidence import RUNS, Audit
from interfaze.replay.executor import Replayer
from interfaze.replay.result import BusinessOutcome, Failed, InvalidInput, Success

from tests.conftest import ENV
GOAL = {
    "capability": "core_banking.lookup_member_balance", "title": "Look up a member's savings balance",
    "description": "Returns the member's name and savings balance.", "product": "mock-core-banking",
    "goal": "Sign on, look up member {{inputs.memberId}}, read name and savings balance.",
    "inputs": {"memberId": {"type": "string", "description": "5-digit member number", "pattern": "^[0-9]{5}$",
                            "sensitive": True, "identifies": "member"}},
    "secrets": ["APP_USER", "APP_PASSWORD"],
    "probes": [
        {"inputs": {"memberId": "99999"}, "why": "a member number that doesn't exist"},
        {"inputs": {"memberId": "01234"}, "why": "a malformed member number"},
        {"inputs": {"memberId": "45678"}, "why": "a restricted member"},
    ],
}


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
    # The page has two "Search" buttons; this is the one after the Member # field
    ("click", call(ref=ref(r'textbox "Member #"[\s\S]*?button "Search"'), intent="submit the member search")),
    ("extract", call(ref=ref(r'cell "Name:"[^\n]*\n\s*- cell'), output="memberName", type="string", intent="read member name")),
    ("extract", call(ref=ref(r'cell "Savings"[^\n]*\n[^\n]*\n\s*- cell'), output="savingsBalance", type="money",
                     intent="read savings balance")),
    ("done", call(success_ref=ref(r'cell "Member Detail"'), summary="read name and balance")),
]


@pytest.fixture(scope="module")
def discovered(base_url, tmp_path_factory):
    # done is refused until the identifying input is verified: the schema requires it (D16)
    script = [*SCRIPT[:-1], SCRIPT[-1],
              ("verify_identity", call(ref=ref(r'cell "Member #:"[^\n]*\n\s*- cell'), input="memberId",
                                       intent="confirm the member number")),
              SCRIPT[-1],
              # The probes: the model names what each stopped screen says. The first proposal quotes the input back,
              # so it's refused and the outcome isn't learned; the third probe needs no model (the library has it).
              ("name_outcome", call(code="MEMBER_NOT_FOUND", kind="business", phrase="No members found",
                                    description="No member has this number.")),
              ("name_outcome", call(code="INVALID_INPUT", kind="business", phrase="Invalid member number 01234",
                                    description="The app rejects the number."))]
    planner = ScriptedPlanner(script)
    out_dir = tmp_path_factory.mktemp("artifacts")
    outcome = asyncio.run(discover(GOAL, base_url, {"memberId": "12345"}, planner=planner, out_dir=out_dir, env=ENV))
    assert outcome["status"] == "recorded", outcome
    return planner, outcome, load_artifact(outcome["artifact"])


@pytest.fixture
def audit(tmp_path):
    return Audit(tmp_path / "audit.jsonl")


@pytest.fixture
def replay(base_url, discovered, audit):
    approved = discovered[2].model_copy(update={"status": "approved"})
    httpx.delete(base_url + "/__faults")

    def run(member: str, fault: str | None = None):
        if fault:
            httpx.post(base_url + "/__faults", params={"name": fault, "times": 1})
        return asyncio.run(Replayer(approved, base_url, {"memberId": member}, env=ENV, audit=audit).run())
    yield run
    httpx.delete(base_url + "/__faults")


def test_discovery_records_a_safe_scoped_artifact(discovered):
    planner, outcome, artifact = discovered
    assert any("no identity checkpoint verifies it" in seen for seen in planner.seen)
    text = Path(outcome["artifact"]).read_text()
    assert "12345" not in text and "demo-only-password" not in text and "teller01" not in text
    assert any(s.value == "{{inputs.memberId}}" for s in artifact.steps)
    # Two buttons share the name, so the recorder scoped it to its fieldset rather than picking by position
    search = next(s for s in artifact.steps if s.id == "submit_member_search")
    assert search.target.within and search.target.within.name == "Member Inquiry"
    assert search.target.nth is None
    # Signing on became a subflow run as a precondition, and the standard outcomes were attached
    assert [s.id for s in artifact.subflows["sign_on"]][0] == "open_sign_on_page"
    assert artifact.preconditions[0].use == "sign_on" and artifact.steps[0].id == "open_member_inquiry"
    assert {o.code for o in artifact.outcomes} >= {"APP_ERROR", "PERMISSION_DENIED", "UNEXPECTED_DIALOG", "SESSION_EXPIRED"}
    # Probes: one learned and verified, one proposal refused, one already covered by the library
    assert [p["result"] for p in outcome["probes"]] == [
        "learned MEMBER_NOT_FOUND",
        "proposal INVALID_INPUT rejected: the phrase contains a number or an input placeholder; use only the fixed wording",
        "already handled as PERMISSION_DENIED"]
    assert "INVALID_INPUT" not in {o.code for o in artifact.outcomes}
    # The probe values were masked from the model like any input
    assert not any("99999" in seen or "01234" in seen for seen in planner.seen)

    # The model never saw the member number, the credentials, or the record's data (§6): placeholders only
    for raw in ("12345", "teller01", "Jane Doe", "4,210.55", "812.07", "0001-"):
        assert not any(raw in seen for seen in planner.seen), raw
    assert any("⟨NAME_1⟩" in seen for seen in planner.seen)
    # The run log keeps neither those nor the values read off the screen
    log = (RUNS / outcome["run_id"] / "events.jsonl").read_text()
    for value in ("12345", "teller01", "demo-only-password", "Jane Doe", "4,210.55"):
        assert value not in log, value


def test_replay_other_member(replay):
    other = replay("34567")
    assert isinstance(other, Success), other
    assert other.outputs == {"memberName": "Mei Chen", "savingsBalance": "2500.00"}
    assert not other.drift and not other.recovered
    replay_log = "".join(p.read_text() for p in (RUNS / other.run_id).glob("*.json*"))
    assert "Mei Chen" not in replay_log and "2500.00" not in replay_log and "34567" not in replay_log


def test_bad_input_never_reaches_the_app(replay):
    assert isinstance(replay("1234"), InvalidInput)


def test_session_timeout_signs_on_again_and_restarts(replay):
    result = replay("12345", "session_timeout")
    assert isinstance(result, Success), result
    assert [e.code for e in result.recovered] == ["SESSION_EXPIRED"]
    assert result.outputs["memberName"] == "Jane Doe"


@pytest.mark.parametrize("member, fault, status, code", [
    ("45678", None, "business_outcome", "PERMISSION_DENIED"),
    ("12345", "server_error", "failed", "APP_ERROR"),
    ("12345", "unknown_dialog", "failed", "UNEXPECTED_DIALOG"),
])
def test_library_outcomes(replay, member, fault, status, code):
    result = replay(member, fault)
    assert result.status == status, result
    assert (result.code if isinstance(result, BusinessOutcome) else result.outcome_code) == code


def test_wrong_record_is_stopped_before_anything_is_read(replay):
    result = replay("12345", "wrong_record")
    assert isinstance(result, Failed) and result.error_class == "WRONG_RECORD"
    # The gate fires on the first step that touches the record page, not at the end-of-run checkpoint
    assert result.at_step == "read_member_name"
    # The caller learns nothing about whose record it was, not even last-4
    dumped = result.model_dump_json()
    assert "Rafael" not in dumped and "3456" not in dumped
    # Evidence keeps the page's structure but none of its data, and no screenshot
    assert [e.kind for e in result.evidence] == ["a11y_snapshot"]
    snapshot = Path(result.evidence[0].path).read_text()
    assert "Name:" in snapshot and 'columnheader "Balance"' in snapshot
    for data in ("Rafael", "Ortiz", "23456", "15,002.10", "0001-"):
        assert data not in snapshot, data


def test_repeated_wrong_records_open_the_circuit_until_reapproved(replay, audit):
    for _ in range(Audit.TRIP_AFTER):
        assert replay("12345", "wrong_record").error_class == "WRONG_RECORD"
    blocked = replay("12345")  # a clean request: the capability itself is no longer trusted
    assert isinstance(blocked, Failed) and blocked.error_class == "CIRCUIT_OPEN" and blocked.at_step == "preflight"
    audit.record("breaker_reset", "core_banking.lookup_member_balance@1.0.0", by="test")
    assert isinstance(replay("12345"), Success)


def test_probe_learned_outcome_works_for_other_inputs(replay):
    # Learned from 99999; holds for any number that isn't a member
    missing = replay("88888")
    assert isinstance(missing, BusinessOutcome) and missing.code == "MEMBER_NOT_FOUND"


def test_unlearned_outcome_fails_where_it_stopped(replay):
    # The INVALID_INPUT proposal was refused, so until review adds it the page is a hard failure that says where
    invalid = replay("01234")
    assert isinstance(invalid, Failed) and invalid.at_step == "read_member_name"


def test_attended_replay_hands_an_unknown_screen_to_a_person(base_url, discovered, tmp_path):
    from tests.test_handoff import attended_replay, clicks
    approved = discovered[2].model_copy(update={"status": "approved"})

    async def person(hub, intervention, http):
        # This artifact has no wait branches after Search, so the notice shows up as a control it can't find
        assert intervention.step_id == "read_member_name" and intervention.reason.startswith("TARGET_UNRESOLVED")
        await clicks(hub, intervention, http, "Acknowledge")

    result, replayer = asyncio.run(attended_replay(base_url, approved, "12345", person, tmp_path, fault="interstitial"))
    assert isinstance(result, Success) and result.outputs["memberName"] == "Jane Doe", result
    assert '"verdict": "retry step"' in (replayer.log.dir / "events.jsonl").read_text()


def test_replay_survives_a_reshuffled_layout(replay, base_url):
    """Seed 1 swaps frame, fieldset, row and column order and wraps the accounts table twice: labels and headers
    still find the same cells (evals/robustness.py runs this across many seeds)."""
    httpx.post(base_url + "/__layout", params={"seed": 1})
    try:
        result = replay("12345")
    finally:
        httpx.delete(base_url + "/__layout")
    assert isinstance(result, Success), result
    assert result.outputs == {"memberName": "Jane Doe", "savingsBalance": "4210.55"}
