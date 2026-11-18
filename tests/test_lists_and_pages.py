"""A row chosen from several by the input it shows (never by position), and a row pages away (D28)."""
import asyncio

import pytest
from pydantic import ValidationError

from interface.artifact.io import load_artifact
from interface.artifact.schema import RoleStrategy, Step, Target
from interface.policy import PolicyViolation, check_action, classify
from interface.replay.executor import Replayer
from interface.replay.result import Failed, Success
from tests.conftest import ENV

HISTORY = "artifacts/core_banking.find_transaction_by_reference.json"
FIND = "artifacts/core_banking.find_member_by_name.json"
NEXT = Target(frame=["main"], strategies=[RoleStrategy(role="link", name="Next")])


def on(base_url: str, path: str, **steps_update):
    a = load_artifact(path)
    steps = [s.model_copy(update=steps_update) if s.action == "pageUntil" else s for s in a.steps]
    return a.model_copy(update={"steps": steps, "policy": a.policy.model_copy(update={"allowed_origins": [base_url]})})


def replay(base_url, artifact, inputs):
    return asyncio.run(Replayer(artifact, base_url, inputs, env=ENV).run())


def test_a_page_turn_is_read_only_for_a_link():
    assert classify("pageUntil", "link", "Next", paging=True) == "read"
    assert classify("pageUntil", "button", "Next", paging=True) == "reversible"   # a wizard's Next stays reversible
    assert classify("pageUntil", "link", "Delete All", paging=True) == "irreversible"
    with pytest.raises(PolicyViolation):
        check_action(load_artifact(HISTORY).policy, "pageUntil", "button", "Confirm", paging=True)


def test_page_until_is_the_only_loop_and_is_bounded():
    with pytest.raises(ValidationError, match="until and maxPages"):
        Step(id="x", intent="page on", action="pageUntil", target=NEXT, risk="read")
    with pytest.raises(ValidationError, match="until and maxPages"):
        Step(id="x", intent="click", action="click", target=NEXT, until=NEXT, max_pages=3, risk="read")
    with pytest.raises(ValidationError):
        Step(id="x", intent="page on", action="pageUntil", target=NEXT, until=NEXT, max_pages=500, risk="read")


def test_the_row_is_found_on_whichever_page_it_is(base_url):
    history = on(base_url, HISTORY)
    for reference, page in (("REF-0003", 1), ("REF-0025", 3), ("REF-0035", 4)):
        result = replay(base_url, history, {"memberId": "34567", "reference": reference})
        assert isinstance(result, Success), (reference, result)
    missing = replay(base_url, history, {"memberId": "34567", "reference": "REF-0099"})
    assert isinstance(missing, Failed) and "no next page" in missing.observed
    limited = replay(base_url, on(base_url, HISTORY, max_pages=1), {"memberId": "34567", "reference": "REF-0025"})
    assert isinstance(limited, Failed) and "maxPages 1" in limited.observed


def test_a_row_is_picked_by_what_it_shows_never_by_position(base_url):
    find = on(base_url, FIND)
    one = replay(base_url, find, {"lastName": "Chen", "dob": "07/04/1985"})
    assert isinstance(one, Success) and one.outputs["memberName"] == "Mei Chen"
    two = replay(base_url, find, {"lastName": "Chen", "dob": "02/14/1990"})  # Ana and Wei Chen, same birthday
    assert isinstance(two, Failed) and two.error_class == "AMBIGUOUS_TARGET"
