import pytest

from interfaze.artifact.schema import Policy
from interfaze.policy import PolicyViolation, check_action, check_url, classify

POLICY = Policy(allowed_origins=["http://localhost:4100"])


@pytest.mark.parametrize("name", ["Confirm", "Submit Transfer", "Delete member", "Close Account", "Approve", "Post"])
def test_commit_buttons_are_irreversible_and_blocked(name):
    assert classify("click", "button", name) == "irreversible"
    with pytest.raises(PolicyViolation, match="irreversible"):
        check_action(Policy(allowed_origins=["http://x"], max_risk="reversible"), "click", "button", name)


@pytest.mark.parametrize("name", ["Search", "Sign On", "Member Inquiry", "New search", "Sign Off"])
def test_navigation_is_read(name):
    assert classify("click", "button", name) == "read"


def test_reversible_needs_policy_to_allow_it():
    assert classify("click", "button", "Continue") == "reversible"
    with pytest.raises(PolicyViolation, match="read-only"):
        check_action(POLICY, "click", "button", "Continue")
    assert check_action(Policy(allowed_origins=["http://x"], max_risk="reversible"), "click", "button", "Continue") == "reversible"


def test_typing_is_not_a_commit():
    assert classify("type", "textbox", "Confirm password") == "read"


def test_url_allowlist():
    check_url(POLICY, "http://localhost:4100/member?id=1")
    with pytest.raises(PolicyViolation, match="origin"):
        check_url(POLICY, "https://evil.example/")
    with pytest.raises(PolicyViolation, match="path"):
        check_url(Policy(allowed_origins=["http://localhost:4100"], allowed_paths=["/inquiry*"]), "http://localhost:4100/admin")
