"""Tenant overrides: addressed by step id, gated by semver, refused loudly when they don't fit (§7, D8)."""
import json

import pytest

from interfaze.artifact.io import load_artifact
from interfaze.artifact.overrides import OverrideError, apply, propose, satisfies, write_pending
from interfaze.artifact.schema import Override, SetOp, Target

EXAMPLE = load_artifact("artifacts/core_banking.lookup_member_balance.json")


@pytest.mark.parametrize("version, spec, ok", [
    ("1.0.0", ">=1.0.0 <2.0.0", True), ("2.0.0", ">=1.0.0 <2.0.0", False), ("1.4.2", "^1.2.0", True),
    ("1.1.0", "~1.0.0", False), ("0.3.1", "^0.3.0", True), ("0.4.0", "^0.3.0", False), ("3.1.4", "*", True),
])
def test_semver_ranges(version, spec, ok):
    assert satisfies(version, spec) is ok


def with_override(*ops, applies_to=">=1.0.0 <2.0.0"):
    return EXAMPLE.model_copy(update={"overrides": {"b": Override(applies_to=applies_to, ops=list(ops))}})


def test_set_by_step_id_and_value_picked_list_item():
    tenant = apply(with_override(SetOp(step="search_member", path="target.strategies[kind=role].name",
                                       value="Find Member")), "b")
    search = next(s for s in tenant.steps if s.id == "search_member")
    assert search.target.strategies[0].name == "Find Member"
    assert tenant.overrides == {}  # what runs is the tenant's version; the base keeps its overrides
    assert next(s for s in EXAMPLE.steps if s.id == "search_member").target.strategies[0].name == "Search"


def test_other_tenants_and_the_base_are_untouched():
    artifact = with_override(SetOp(step="search_member", path="target.strategies[kind=role].name", value="Find Member"))
    assert apply(artifact, "a") is artifact


def test_an_unknown_step_fails_loudly():
    artifact = with_override(SetOp(step="no_such_step", path="target.frame", value=[]))
    # Loading such an artifact is refused, and so is applying the override should one get past loading
    with pytest.raises(ValueError, match="unknown step no_such_step"):
        type(artifact).model_validate(artifact.model_dump(by_alias=True))
    with pytest.raises(OverrideError, match="doesn't have"):
        apply(artifact, "b")


def test_an_override_outside_its_version_range_is_refused():
    artifact = with_override(SetOp(step="search_member", path="target.frame", value=["main"]), applies_to="^2.0.0")
    with pytest.raises(OverrideError, match="re-target"):
        apply(artifact, "b")


def test_a_path_that_picks_nothing_is_refused():
    with pytest.raises(OverrideError, match="picks 0 items"):
        apply(with_override(SetOp(step="search_member", path="target.strategies[kind=label].label", value="x")), "b")


def test_relabel_keeps_the_role_strategy_first_and_corrects_the_name():
    strategies = [{"kind": "role", "role": "button", "name": "Search"}, {"kind": "text", "text": "Search"},
                  {"kind": "onlyInRegion", "role": "button"}]
    ops = propose("target", strategies, 2, "Find Member")
    assert [(o.path, o.value) for o in ops] == [("target.strategies[kind=role].name", "Find Member"),
                                                ("target.strategies[kind=text].text", "Find Member")]


def test_a_lost_label_moves_the_strategy_that_worked_to_the_front():
    strategies = [{"kind": "role", "role": "textbox", "name": "Member #"}, {"kind": "label", "label": "Member #"},
                  {"kind": "anchor", "anchorText": "Member #", "relation": "right-of", "role": "textbox"}]
    (op,) = propose("target", strategies, 2, "")
    assert op.path == "target.strategies" and [s["kind"] for s in op.value] == ["anchor", "role", "label"]


def test_a_live_name_that_is_data_is_never_proposed():
    (op,) = propose("target", [{"kind": "role", "role": "link", "name": "View"}, {"kind": "text", "text": "View"}],
                    1, "0001-12345-00")
    assert op.path == "target.strategies"


def test_pending_override_replaces_an_earlier_proposal_for_the_same_step(tmp_path):
    path = tmp_path / "b.json"
    write_pending(path, EXAMPLE, [SetOp(step="search_member", path="target.strategies[kind=role].name", value="Find")])
    write_pending(path, EXAMPLE, [SetOp(step="search_member", path="target.strategies[kind=role].name", value="Find Member")])
    pending = json.loads(path.read_text())
    assert pending["appliesTo"] == ">=1.0.0 <2.0.0" and [o["value"] for o in pending["ops"]] == ["Find Member"]


def test_only_in_region_needs_a_region():
    with pytest.raises(ValueError, match="needs the target's `within`"):
        Target(frame=["main"], strategies=[{"kind": "onlyInRegion", "role": "button"}])
