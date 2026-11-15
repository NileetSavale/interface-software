"""The page map (§7a): screens without data, edges from verified runs only, and the two places it's read: discovery's
known actions and replay's bounded fallback."""
import asyncio
import json
import re

import httpx
import pytest

from interface.discovery.loop import discover
from interface.discovery.planner import ScriptedPlanner
from interface.evidence import Audit
from interface.pagemap import PageMap, screen_of
from interface.replay.executor import Replayer
from interface.replay.result import Failed, Success
from tests.conftest import ENV
from tests.test_end_to_end import GOAL, SCRIPT, call, discovered, ref  # noqa: F401  (discovered is a fixture)

DETAIL = """- table [ref=e1]:
  - row [ref=e2]:
    - cell "Member #:" [ref=e3]
    - cell "{id}" [ref=e4]
  - row [ref=e5]:
    - cell "Name:" [ref=e6]
    - cell "{name}" [ref=e7]
- textbox "Note" [ref=e8]: {note}"""


def test_a_screen_is_its_structure_not_its_data():
    jane = screen_of(DETAIL.format(id="12345", name="Jane Doe", note="called today"))
    mei = screen_of(DETAIL.format(id="34567", name="Mei Chen", note=""))
    assert jane.id == mei.id  # two members' detail pages are one screen, and what's typed in a field isn't the screen
    assert "Name:" in jane.labels and "Member #:" in jane.labels
    assert not any(v in " ".join(jane.labels) for v in ("Jane", "12345", "called"))
    assert screen_of(DETAIL.replace("Name:", "Branch:").format(id="1", name="x", note="")).id != jane.id


def lookup_script(member_ref=r'textbox "Member #"', search=r'textbox "Member #"[\s\S]*?button "Search"',
                  menu=r'link "Member Inquiry"'):
    steps = [*SCRIPT[:3],
             ("click", call(ref=ref(menu), intent="open member inquiry")),
             ("type_text", call(ref=ref(member_ref), text="{{inputs.memberId}}", intent="enter the member number")),
             ("click", call(ref=ref(search), intent="submit the member search")),
             *SCRIPT[6:-1]]
    return [*steps, ("verify_identity", call(ref=ref(r'cell "Member #:"[^\n]*\n\s*- cell'), input="memberId",
                                             intent="confirm the member number")), SCRIPT[-1]]


def run_discovery(base_url, script, page_map, tmp_path):
    goal = {k: v for k, v in GOAL.items() if k != "probes"}
    planner = ScriptedPlanner(script)
    outcome = asyncio.run(discover(goal, base_url, {"memberId": "12345"}, planner=planner, out_dir=tmp_path, env=ENV,
                                   escalation_model=None, page_map=page_map))
    return outcome, planner


def test_discovery_learns_the_map_and_a_second_discovery_runs_known_actions_in_one_turn(base_url, tmp_path):
    bank = PageMap(tmp_path / "bank-a.json", bank="a")
    first, _ = run_discovery(base_url, lookup_script(), bank, tmp_path / "1")
    assert first["status"] == "recorded" and len(bank.data["edges"]) == 6
    raw = json.dumps(bank.data)
    assert not any(v in raw for v in ("Jane", "12345", "teller01", "demo-only", "4,210"))  # structure only

    # The second run is offered the known actions, and does sign-on, the menu and the search in one call
    every_known = lambda screen: re.findall(r"^\s*(k\d+):", screen, re.M)
    script = [("follow_known", call(ids=every_known)), *lookup_script()[6:]]
    second, planner = run_discovery(base_url, script, bank, tmp_path / "2")
    assert second["status"] == "recorded", second
    assert "Known actions (from verified runs on this app" in planner.seen[0]
    assert second["known_actions_used"] == 6
    turns_first, turns_second = len(lookup_script()), len(script)
    assert turns_second < turns_first - 4

    # What follow_known recorded is an ordinary artifact: it replays for another member
    from interface.artifact.io import load_artifact
    artifact = load_artifact(second["artifact"]).model_copy(update={"status": "approved"})
    assert {s.recorded_by for s in artifact.all_steps()} >= {"map"}
    result = asyncio.run(Replayer(artifact, base_url, {"memberId": "34567"}, env=ENV,
                                  audit=Audit(tmp_path / "a.jsonl")).run())
    assert isinstance(result, Success), result


def test_an_unverified_run_adds_nothing(base_url, tmp_path):
    bank = PageMap(tmp_path / "bank.json", bank="a")
    outcome, _ = run_discovery(base_url, [*lookup_script()[:6], ("give_up", call(reason="stop"))], bank, tmp_path)
    assert outcome["status"] == "gave_up" and bank.data["edges"] == {}


def test_replay_on_a_bank_where_no_strategy_survives_falls_back_on_the_map(base_url, discovered, tmp_path):
    """Bank C renames the menu's "Member Inquiry" to "Member Lookup": every recorded strategy for that step misses.
    Without a map the run stops there. With bank C's map (learned from one verified run on C) replay follows the
    known route, checks the next step's control is there, and reports MAP_FALLBACK with the map version it used."""
    approved = discovered[2].model_copy(update={"status": "approved"})
    bank_c = PageMap(tmp_path / "bank-c.json", bank="c")
    httpx.post(base_url + "/__variant", params={"name": "c"})
    try:
        learned, _ = run_discovery(base_url, lookup_script(
            member_ref=r'cell "Member #"[^\n]*\n[^\n]*\n\s*- textbox', search=r'button "Find Member"',
            menu=r'link "Member Lookup"'), bank_c, tmp_path / "c")
        assert learned["status"] == "recorded", learned

        def replay(page_map):
            return asyncio.run(Replayer(approved, base_url, {"memberId": "34567"}, env=ENV, tenant="c",
                                        pending_dir=tmp_path / "pending", audit=Audit(tmp_path / "a.jsonl"),
                                        page_map=page_map).run())
        # Without a map C fails at once: even signing on is proved by the menu link it renamed
        without = replay(None)
        assert isinstance(without, Failed) and (without.at_step, without.error_class) == ("sign", "EXPECT_FAILED")

        version = bank_c.version()
        with_map = replay(bank_c)
        assert isinstance(with_map, Success) and with_map.outputs["memberName"] == "Mei Chen", with_map
        # Two map events: it confirmed the screen sign-on leads to, then found the renamed link
        by_step = {r.step_id: r for r in with_map.recovered if r.code == "MAP_FALLBACK"}
        assert set(by_step) == {"sign", "open_member_inquiry"}
        assert all(r.map_version == version for r in by_step.values())
        assert "Member Lookup" in by_step["open_member_inquiry"].action
        # The map's fix is proposed as an override for C, for a person to approve
        proposal = json.loads((tmp_path / "pending" / "c.json").read_text())
        assert any(op["step"] == "open_member_inquiry" and op["path"] == "target" for op in proposal["ops"])
    finally:
        httpx.post(base_url + "/__variant", params={"name": "a"})


def test_the_fallback_never_takes_an_untrusted_or_risky_edge(tmp_path):
    bank = PageMap(tmp_path / "m.json", bank="a")
    t = {"from": "s1", "from_labels": [], "target": {"frame": [], "strategies": [{"kind": "role", "role": "link",
         "name": "X"}]}, "value": None, "intent": "go", "to": "s2", "to_labels": ["Next"]}
    bank.learn([{**t, "action": "click", "risk": "read"}], "human", verified=True)            # weight 0.2
    bank.learn([{**t, "action": "click", "risk": "reversible", "intent": "save", "target": {**t["target"],
                 "strategies": [{"kind": "role", "role": "button", "name": "Save"}]}}], "replay", verified=True)
    assert bank.paths("s1", min_weight=0.5, risks=("read",)) == []          # one is untrusted, the other changes data
    bank.learn([{**t, "action": "click", "risk": "irreversible", "intent": "confirm"}], "replay", verified=True)
    assert all(e["risk"] != "irreversible" for e in bank.data["edges"].values())   # never stored at all
    bank.learn([{**t, "action": "click", "risk": "read", "value": "Jane Doe"}], "replay", verified=True)
    assert "Jane Doe" not in json.dumps(bank.data)                          # a literal typed value is never kept
