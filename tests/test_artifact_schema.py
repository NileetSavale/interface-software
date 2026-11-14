"""Each rule the artifact schema enforces, tested by breaking the documented example in exactly one way."""
import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from interfaze.artifact.io import dump_artifact, load_artifact, strip_comments
from interfaze.artifact.schema import Artifact

EXAMPLE = Path(__file__).parent.parent / "docs" / "artifact-example.lookup_member_balance.jsonc"


@pytest.fixture
def example() -> dict:
    return json.loads(strip_comments(EXAMPLE.read_text()))


def step(data: dict, step_id: str) -> dict:
    return next(s for s in data["steps"] if s["id"] == step_id)


def rejects(data: dict, message: str):
    with pytest.raises(ValidationError) as e:
        Artifact.model_validate(data)
    assert message in str(e.value), str(e.value)


def test_documented_example_is_valid():
    artifact = load_artifact(EXAMPLE)
    assert artifact.capability.id == "core_banking.lookup_member_balance"
    assert artifact.inputs["memberId"].identifies == "member"


def test_round_trip_is_lossless(example):
    artifact = Artifact.model_validate(example)
    again = Artifact.model_validate_json(dump_artifact(artifact))
    assert again == artifact


def test_comment_stripping_keeps_urls_inside_strings():
    assert json.loads(strip_comments('{"a": "http://x/y"} // note')) == {"a": "http://x/y"}


# D1: an artifact can reach a commit point but never press it
def test_irreversible_step_is_rejected(example):
    step(example, "submit_search")["risk"] = "irreversible"
    rejects(example, "'read' or 'reversible'")


def test_reversible_step_needs_policy_to_allow_it(example):
    step(example, "submit_search")["risk"] = "reversible"
    rejects(example, "reversible but the policy allows read only")
    example["policy"]["maxRisk"] = "reversible"
    Artifact.model_validate(example)


# D16: an input that names a record must be checked against the record on screen
def test_identifying_input_requires_identity_checkpoint(example):
    example["success"]["checkpoint"] = [c for c in example["success"]["checkpoint"] if c["kind"] != "identity"]
    rejects(example, "input memberId identifies a member but no identity checkpoint verifies it")


def test_identity_checkpoint_must_name_a_declared_input(example):
    next(c for c in example["success"]["checkpoint"] if c["kind"] == "identity")["input"] = "accountId"
    rejects(example, "identity checkpoint refers to undeclared input accountId")


# §2 ambiguity rule: position is a last resort and is always reviewed
def test_nth_target_requires_review_flag(example):
    step(example, "submit_search")["target"]["nth"] = 1
    rejects(example, "must be flagged needsReview")
    step(example, "submit_search")["needsReview"] = True
    Artifact.model_validate(example)


# D8: overrides address steps by id and fail loudly when the id is gone
def test_override_on_unknown_step_is_rejected(example):
    example["overrides"]["variant:b"]["ops"][0]["step"] = "submit_serch"
    rejects(example, "override variant:b targets unknown step submit_serch")


def test_override_by_array_index_is_not_a_thing(example):
    example["overrides"]["variant:b"]["ops"][0] = {"op": "replace", "path": "/steps/2/target", "value": "x"}
    rejects(example, "op")


# References between parts of the artifact all resolve
def test_unknown_input_template_is_rejected(example):
    step(example, "enter_member_id")["value"] = "{{inputs.memberID}}"
    rejects(example, "template refers to undeclared input memberID")


def test_secret_templates_are_allowed_without_declaration(example):
    Artifact.model_validate(example)  # login uses {{secrets.APP_USER}}
    assert any("secrets.APP_USER" in s.value for s in Artifact.model_validate(example).subflows["login"])


def test_output_without_extractor_is_rejected(example):
    example["outputs"]["checkingBalance"] = {"type": "money"}
    rejects(example, "output checkingBalance has no extractor")


def test_extractor_without_declared_output_is_rejected(example):
    del example["outputs"]["memberName"]
    rejects(example, "extractor memberName isn't a declared output")


def test_outcome_attached_to_unknown_step_is_rejected(example):
    example["outcomes"][0]["at"] = "after:submit"
    rejects(example, "attached to unknown step submit")


def test_wait_branch_on_undeclared_outcome_is_rejected(example):
    step(example, "submit_search")["waitFor"]["anyOf"][1]["code"] = "NO_SUCH_MEMBER"
    rejects(example, "waits for undeclared outcome NO_SUCH_MEMBER")


def test_recovery_with_unknown_subflow_is_rejected(example):
    next(o for o in example["outcomes"] if o["code"] == "SESSION_EXPIRED")["recovery"]["do"]["subflow"] = "relogin"
    rejects(example, "recovers with unknown subflow relogin")


def test_duplicate_step_ids_are_rejected(example):
    example["steps"].append(copy.deepcopy(step(example, "read_balance")))
    rejects(example, "step id read_balance is used more than once")


# The error taxonomy is declared, not implied
def test_recoverable_outcome_must_say_how_to_recover(example):
    del next(o for o in example["outcomes"] if o["code"] == "SESSION_EXPIRED")["recovery"]
    rejects(example, "recoverable outcomes, and only they, declare a recovery")


def test_business_outcome_cannot_declare_a_recovery(example):
    example["outcomes"][0]["recovery"] = {"do": {"kind": "wait", "timeoutMs": 1000}}
    rejects(example, "recoverable outcomes, and only they, declare a recovery")


def test_recovery_attempts_are_bounded(example):
    next(o for o in example["outcomes"] if o["code"] == "SESSION_EXPIRED")["recovery"]["maxAttempts"] = 5
    rejects(example, "less than or equal to 3")


# Step shape follows its action
def test_type_step_needs_a_value(example):
    del step(example, "enter_member_id")["value"]
    rejects(example, "type needs a value")


def test_click_step_needs_a_target(example):
    del step(example, "open_inquiry")["target"]
    rejects(example, "click needs a target")


def test_invalid_regex_is_rejected_at_load(example):
    example["outcomes"][0]["detect"]["text"]["matches"] = "(unclosed"
    rejects(example, "not a valid regex")


def test_empty_visible_condition_is_rejected(example):
    step(example, "open_inquiry")["expect"] = {"frame": ["main"]}
    rejects(example, "needs at least one of role, name, label, text")


def test_unknown_fields_are_errors_not_ignored(example):
    step(example, "open_inquiry")["retries"] = 3
    rejects(example, "Extra inputs are not permitted")
