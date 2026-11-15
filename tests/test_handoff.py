"""Handing the live session to a person and back (docs/ARCHITECTURE.md §5). The operator is simulated, but through
the real channel: control goes over the operator page's HTTP endpoints, and the "human" clicks in the automation's
own browser, where the injected listeners record it."""
import asyncio

import httpx
import pytest

from interface.artifact.io import load_artifact
from interface.evidence import Audit, RunLog
from interface.operator import OperatorHub
from interface.replay.executor import Replayer
from interface.replay.result import Failed, Success
from interface.session import ControlError, ControlLost, Session
from tests.conftest import ENV, free_port

EXAMPLE = "docs/artifact-example.lookup_member_balance.jsonc"


def artifact_without(base_url: str, *codes: str):
    """The documented example, minus outcomes, so the screen they'd handle is one the artifact doesn't know."""
    a = load_artifact(EXAMPLE)
    return a.model_copy(update={
        "status": "approved", "outcomes": [o for o in a.outcomes if o.code not in codes],
        "policy": a.policy.model_copy(update={"allowed_origins": [base_url]})})


async def attended_replay(base_url, artifact, member, operator, tmp_path, fault=None, pause_timeout_s=20):
    """Runs a replay with the operator page up; `operator(hub, intervention, http)` plays the person."""
    httpx.delete(base_url + "/__faults")
    if fault:
        httpx.post(base_url + "/__faults", params={"name": fault, "times": 1})
    hub, port = OperatorHub(), free_port()
    await hub.start(port)
    tasks = []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
        hub.on_publish = lambda i: tasks.append(asyncio.create_task(operator(hub, i, http)))
        replayer = Replayer(artifact, base_url, {"memberId": member}, env=ENV, audit=Audit(tmp_path / "audit.jsonl"),
                            operator_hub=hub, pause_timeout_s=pause_timeout_s)
        result = await replayer.run()
        for t in tasks:
            await t
    await hub.stop()
    return result, replayer


def main_frame(hub, intervention):
    return hub.closed.get(intervention.id) or hub.open[intervention.id][2].page.frame("main")


async def clicks(hub, intervention, http, button):
    """A person takes control, clicks `button` in the live browser, and hands back."""
    page = hub.open[intervention.id][2].page
    assert (await http.post(f"/interventions/{intervention.id}/claim", params={"operator": "alice"})).status_code == 200
    # Nobody else may hand back a session alice holds
    assert (await http.post(f"/interventions/{intervention.id}/handback", params={"operator": "bob"})).status_code == 409
    state = (await http.get(f"/interventions/{intervention.id}/state")).json()
    assert state["state"] == "human" and state["holder"] == "operator:alice"
    assert (await http.get(f"/interventions/{intervention.id}/screenshot")).headers["content-type"] == "image/png"
    await page.frame("main").get_by_role("button", name=button).click()
    await asyncio.sleep(0.3)
    assert (await http.post(f"/interventions/{intervention.id}/handback", params={"operator": "alice"})).status_code == 200


def test_unknown_notice_is_handed_to_a_person_and_the_run_finishes(base_url, tmp_path):
    artifact = artifact_without(base_url, "NOTICE_INTERSTITIAL")

    async def person(hub, intervention, http):
        # The example waits for one of its known screens after Search; the notice is none of them
        assert intervention.step_id == "submit_search" and intervention.reason.startswith("TIMEOUT")
        await clicks(hub, intervention, http, "Acknowledge")

    result, replayer = asyncio.run(attended_replay(base_url, artifact, "12345", person, tmp_path, fault="interstitial"))
    assert isinstance(result, Success), result
    assert result.outputs["memberName"] == "Jane Doe"
    events = (replayer.log.dir / "events.jsonl").read_text()
    # The handoff is in the log: every transition with its epoch, the person's click, and the resume verdict
    assert '"to": "paused_for_human"' in events and '"to": "human"' in events and '"to": "resuming"' in events
    assert '"event": "human_action"' in events and '"name": "Acknowledge"' in events
    assert '"verdict": "step had already acted"' in events


def test_unexpected_dialog_resumes_only_once_it_is_gone(base_url, tmp_path):
    artifact = artifact_without(base_url)
    attempts = []

    async def person(hub, intervention, http):
        attempts.append(intervention.reason)
        if len(attempts) == 1:
            # Hands back without dealing with it: the resume check refuses and the run pauses again
            await http.post(f"/interventions/{intervention.id}/claim", params={"operator": "alice"})
            await http.post(f"/interventions/{intervention.id}/handback", params={"operator": "alice"})
            for _ in range(50):
                if (await http.get(f"/interventions/{intervention.id}/state")).json()["state"] == "paused_for_human":
                    break
                await asyncio.sleep(0.1)
            await clicks(hub, intervention, http, "OK")

    result, replayer = asyncio.run(attended_replay(base_url, artifact, "12345", person, tmp_path, fault="unknown_dialog"))
    assert isinstance(result, Success), result
    events = (replayer.log.dir / "events.jsonl").read_text()
    assert "resume check failed" in events and '"verdict": "step had already acted"' in events


def test_operator_can_abort(base_url, tmp_path):
    artifact = artifact_without(base_url)

    async def person(hub, intervention, http):
        await http.post(f"/interventions/{intervention.id}/abort", params={"operator": "alice"})

    result, _ = asyncio.run(attended_replay(base_url, artifact, "12345", person, tmp_path, fault="unknown_dialog"))
    assert isinstance(result, Failed) and result.error_class == "OPERATOR_ABORTED"


def test_nobody_comes(base_url, tmp_path):
    artifact = artifact_without(base_url)

    async def nobody(hub, intervention, http):
        pass

    result, _ = asyncio.run(attended_replay(base_url, artifact, "12345", nobody, tmp_path, fault="unknown_dialog",
                                            pause_timeout_s=1))
    assert isinstance(result, Failed) and result.error_class == "ESCALATION_UNATTENDED"


def test_an_action_decided_before_a_handoff_is_dropped(tmp_path):
    session = Session(RunLog("test_control", root=tmp_path))
    decided_in = session.epoch
    session.check(decided_in)  # holds control: fine
    from interface.session import Intervention
    session.pause(Intervention(id="i", run_id="r", subject="s", step_id="x", intent="y", reason="z", since="", snapshot=""))
    session.claim("alice")
    with pytest.raises(ControlLost):
        session.check(decided_in)  # the in-flight click lands after control passed to alice: dropped
    with pytest.raises(ControlError):
        session.hand_back("bob")
    session.hand_back("alice")
    with pytest.raises(ControlLost):
        session.check(decided_in)  # still the old epoch, even though automation holds control again
    session.resume_verified()
    session.check(session.epoch)


def test_discovery_ladder_ends_with_a_person_teaching_the_step(base_url, tmp_path):
    from interface.discovery.loop import discover
    from interface.discovery.planner import ScriptedPlanner
    from tests.test_end_to_end import GOAL, SCRIPT, call, ref

    script = [*SCRIPT[:6],  # sign on, open inquiry, search; the notice page appears
              ("give_up", call(reason="an unfamiliar notice page")),   # Sonnet: the ladder switches to Opus
              ("give_up", call(reason="still can't place this notice")),  # Opus: the ladder hands to a person
              *SCRIPT[6:8],
              ("verify_identity", call(ref=ref(r'cell "Member #:"[^\n]*\n\s*- cell'), input="memberId",
                                       intent="confirm the member number")),
              SCRIPT[-1]]
    planner = ScriptedPlanner(script, model="claude-sonnet-5-5")
    seen_models = []

    async def run():
        hub = OperatorHub()
        tasks = []

        async def person(intervention):
            _, session, surface = hub.open[intervention.id]
            seen_models.append(planner.model)
            session.claim("alice")
            await surface.page.frame("main").get_by_role("button", name="Acknowledge").click()
            await asyncio.sleep(0.3)
            session.hand_back("alice")

        hub.on_publish = lambda i: tasks.append(asyncio.create_task(person(i)))
        goal = {k: v for k, v in GOAL.items() if k != "probes"}
        outcome = await discover(goal, base_url, {"memberId": "12345"}, planner=planner, out_dir=tmp_path, env=ENV,
                                 operator_hub=hub, pause_timeout_s=20)
        for t in tasks:
            await t
        return outcome

    httpx.delete(base_url + "/__faults")
    httpx.post(base_url + "/__faults", params={"name": "interstitial", "times": 1})
    try:
        outcome = asyncio.run(run())
    finally:
        httpx.delete(base_url + "/__faults")
    assert outcome["status"] == "recorded", outcome
    assert seen_models == ["claude-opus-5-5"]  # the person was the third rung, after the model switch
    artifact = load_artifact(outcome["artifact"])
    taught = [s for s in artifact.steps if s.recorded_by == "human"]
    assert len(taught) == 1 and taught[0].needs_review and taught[0].target.strategies[0].name == "Acknowledge"


def test_invoke_returns_needs_human_at_the_deadline_and_the_run_finishes_later(base_url, tmp_path):
    from interface.replay.result import NeedsHuman
    from interface.runs import Runs
    artifact = artifact_without(base_url)

    async def run():
        hub = OperatorHub()
        runs = Runs(operator_hub=hub)
        published = asyncio.Event()
        hub.on_publish = lambda i: published.set()
        first = await runs.invoke(artifact, base_url, {"memberId": "12345"}, deadline_s=8, env=ENV,
                                  audit=Audit(tmp_path / "audit.jsonl"), pause_timeout_s=30)
        # The caller has its answer for now; the run is still alive, paused for a person
        assert isinstance(first, NeedsHuman) and first.reason.startswith("UNEXPECTED_DIALOG"), first
        assert runs.get_result(first.run_id).status == "needs_human"
        intervention, session, surface = hub.open[first.intervention_id]
        session.claim("alice")
        await surface.page.frame("main").get_by_role("button", name="OK").click()
        session.hand_back("alice")
        return await runs.wait(first.run_id, 30)

    httpx.delete(base_url + "/__faults")
    httpx.post(base_url + "/__faults", params={"name": "unknown_dialog", "times": 1})
    late = asyncio.run(run())
    assert isinstance(late, Success) and late.outputs["memberName"] == "Jane Doe", late
