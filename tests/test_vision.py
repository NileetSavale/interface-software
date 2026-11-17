"""The pixels-only surface, end to end with a scripted planner in place of Claude: discover on the canvas client by
OCR, record an artifact, replay it with no model through faults, on a second bank with another look and labels,
against the commit guard, and check what's read is never guessed."""
import asyncio
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import uvicorn

from apps.mock_gui.app import BANK
from interface.artifact.io import load_artifact
from interface.artifact.schema import Policy
from interface.discovery.loop import discover
from interface.discovery.planner import ScriptedPlanner
from interface.evidence import RUNS, Audit
from interface.replay.executor import Replayer
from interface.replay.result import BusinessOutcome, Failed, Success
from interface.surface import vision
from interface.surface.vision import Unreadable, VisionSurface
from tests.conftest import ENV, free_port
from tests.test_end_to_end import call, ref

ROOT = Path(__file__).resolve().parent.parent
GOAL = {k: v for k, v in json.loads((ROOT / "goals" / "vision_lookup_member_balance.json").read_text()).items()
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
    ("done", call(success_ref=ref(r'heading "Member Detail"'), summary="read name and balance")),
]


@pytest.fixture(scope="module")
def host():
    server = uvicorn.Server(uvicorn.Config("apps.mock_gui.app:app", port=free_port(), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield BANK, f"http://127.0.0.1:{server.config.port}"
    server.should_exit = True


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


def test_discovery_from_pixels_records_the_same_kind_of_artifact(discovered):
    planner, outcome, artifact = discovered
    assert artifact.app.surface == "vision"
    text = Path(outcome["artifact"]).read_text()
    assert "12345" not in text and "teller01" not in text and "demo-only-password" not in text
    assert artifact.preconditions[0].use == "sign_on"
    member = next(s for s in artifact.steps if s.value == "{{inputs.memberId}}")
    assert member.target.frame == ["CB0300"]
    assert [s.kind for s in member.target.strategies] == ["role", "label", "anchor", "bbox"]
    # The model saw the OCR'd snapshot through the masker, never the record, and never a picture
    for raw in ("12345", "teller01", "Jane Doe", "4,210.55", "812.07", "04/01/1980"):
        assert not any(raw in seen for seen in planner.seen), raw
    log = (RUNS / outcome["run_id"] / "events.jsonl").read_text()
    for value in ("12345", "teller01", "demo-only-password", "Jane Doe", "4,210.55"):
        assert value not in log, value


def test_replay_another_member_from_pixels_with_no_model(replay):
    result = replay("34567")
    assert isinstance(result, Success), result
    assert result.outputs == {"memberName": "Mei Chen", "savingsBalance": "2500.00"}
    assert not result.drift and not result.recovered


def test_the_error_taxonomy_holds_on_pixels(replay):
    assert isinstance(r := replay("45678"), BusinessOutcome) and r.code == "PERMISSION_DENIED", r
    recovered = replay("23456", fault="session_timeout")
    assert isinstance(recovered, Success) and [e.code for e in recovered.recovered] == ["SESSION_EXPIRED"], recovered
    assert isinstance(r := replay("23456", fault="wrong_record"), Failed) and r.error_class == "WRONG_RECORD", r
    late = replay("23456", fault="late_fill")
    assert isinstance(late, Success) and late.outputs["memberName"] == "Rafael Ortiz", late


def test_a_second_bank_with_another_look_and_labels(replay):
    result = replay("34567", variant="b", tenant="b")
    assert isinstance(result, Success), result
    assert result.outputs["memberName"] == "Mei Chen"
    drifted = {d.step_id: d.strategy for d in result.drift}
    assert drifted == {"enter_member_number": "bbox", "submit_member_search": "bbox"}, drifted


def test_a_value_that_cant_be_read_twice_alike_fails_the_step(replay, monkeypatch):
    original = vision._recognise

    def second_read_differs(image, scale):  # only a value's check read is at scale 2
        text, confidence = original(image, scale)
        return (text.replace("0", "8"), confidence) if scale == 2 else (text, confidence)
    monkeypatch.setattr(vision, "_recognise", second_read_differs)
    result = replay("34567")  # 34567 and Mei Chen read alike both ways; 2,500.00 doesn't
    assert isinstance(result, Failed) and result.error_class == "UNREADABLE", result
    assert result.at_step == "read_savings_balance" or "savings" in result.at_step, result


def test_a_confusable_read_is_refused(monkeypatch):
    surface = VisionSurface()
    monkeypatch.setattr(vision, "_recognise", lambda image, scale: ("4,21O.55", 0.99))
    surface.image = np.zeros((100, 100, 3), dtype=np.uint8)
    surface.pixels = type("P", (), {"scale": 1.0})()
    element = vision.Element("text", "4,210.55", 0, 10, 40, screen="CB0310", box=(10, 10, 40, 14))
    surface._parsed[surface._hash] = [element]

    async def quiet(wait_s: float = 2.0):
        return None
    surface._quiet = quiet
    with pytest.raises(Unreadable):
        asyncio.run(surface.read_text(element))


def test_post_is_never_pressed_from_pixels(host):
    bank, base = host

    async def go():
        surface = VisionSurface(policy=Policy(allowed_origins=[base], commit_paths=["/CB0410"]))
        await surface.navigate(base + "/")

        async def do(role, name, value=None):
            await surface.observe()
            r = next(r for r in surface._refs if surface.ref_line(r) == (role, name))
            await surface.act_on_ref(r, "type" if value is not None else "click", value)
        await do("textbox", "User ID", "teller01")
        await do("textbox", "Password", "demo-only-password")
        await do("button", "Sign on")
        await do("textbox", "Option", "2")
        await do("button", "Select")
        for name, value in (("Member #", "12345"), ("Account type", "Savings"), ("Nickname", "Trip"),
                            ("Initial deposit", "100.00")):
            await do("textbox", name, value)
        await do("button", "Review")
        on = (await surface.observe()).url
        await do("button", "Post")
        still = (await surface.observe()).url
        shot = await surface.screenshot(str(RUNS / "vision-guard.png"))
        await surface.close()
        return on, still, list(surface.blocked), shot

    before = bank.confirmed
    on, still, blocked, shot = asyncio.run(go())
    assert on.endswith("/CB0410") and still.endswith("/CB0410") and blocked and "'Post'" in blocked[0], blocked
    assert bank.confirmed == before
    assert shot  # evidence, with every value blacked out


def test_the_evidence_screenshot_blacks_out_values(host, tmp_path):
    _, base = host

    async def go():
        surface = VisionSurface()
        await surface.navigate(base + "/?values=11")
        observation = await surface.observe()
        path = await surface.screenshot(str(tmp_path / "masked.png"))
        await surface.close()
        return observation.text, path
    shown, path = asyncio.run(go())
    from PIL import Image
    masked = VisionSurface()
    masked.image = np.array(Image.open(path).convert("RGB"))
    words = " ".join(w.text for w in vision.Reader.words(masked.image))
    assert "Savings balance" in words.replace("  ", " ") or "Savingsbalance" in words  # labels stay
    assert not any(ch.isdigit() for w in vision.Reader.words(masked.image) for ch in w.text
                   if not w.text.startswith("CB")), words  # every number is gone


def test_spaces_come_back_only_where_they_cant_change_a_value():
    # OCR drops spaces; the pixels put them back, and a value keeps one only between letters
    assert vision._merge_spaces("JaneDoe", "Jane Doe") == "Jane Doe"
    assert vision._merge_spaces("Member #", "Member#") == "Member #"
    assert vision._letters_only("Jane O'Brien") == "Jane O'Brien"
    assert vision._letters_only("4, 210.55") == "4,210.55" and vision._letters_only("12 345") == "12345"


def test_a_masked_field_is_counted_by_its_glyphs_not_read():
    # OCR read sixteen of ParaBank's password dots as "FE00-00000000000"; the dots are counted instead
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (220, 24), "white")
    draw = ImageDraw.Draw(image)
    for i in range(16):
        draw.ellipse((6 + i * 9, 9, 11 + i * 9, 14), fill="black")
    assert vision._mask_glyphs(np.array(image)) == 16
    words = Image.new("RGB", (220, 24), "white")
    ImageDraw.Draw(words).text((6, 4), "teller01", fill="black")
    assert vision._mask_glyphs(np.array(words)) == 0
