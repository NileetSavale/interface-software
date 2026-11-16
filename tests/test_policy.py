import pytest

from interface.artifact.schema import Policy
from interface.policy import PolicyViolation, check_action, check_url, classify

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


def test_network_gate_refuses_commit_paths_and_other_origins():
    from interface.policy import refused
    policy = Policy(allowed_origins=["http://localhost:4100"], commit_paths=["/subaccount/confirm"])
    assert "commits a change" in refused(policy, "http://localhost:4100/subaccount/confirm", document=True)
    assert "origin" in refused(policy, "http://verify-member.example.net/start", document=True)
    # A page's own subresource elsewhere is still kept on the allowlist; data: never leaves the browser
    assert "origin" in refused(policy, "https://cdn.example.com/x.png", document=False)
    assert refused(policy, "data:image/png;base64,AAAA", document=False) is None
    assert refused(policy, "http://localhost:4100/subaccount/review", document=True) is None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "FILE:///Users/someone/.ssh/id_rsa", "chrome://settings",
                                 "view-source:http://localhost:4100/", "ftp://localhost:4100/x", "javascript:alert(1)",
                                 "data:text/html,<h1>Sign in again</h1>", "blob:http://localhost:4100/abc"])
def test_only_http_and_https_load_a_page(url):
    """Found in review: any other scheme used to pass, so a tampered artifact could open file:// and read the disk."""
    from interface.policy import refused
    with pytest.raises(PolicyViolation, match="aren't allowed"):
        check_url(POLICY, url)
    if not url.startswith(("data:", "blob:")):
        assert "aren't allowed" in refused(POLICY, url, document=False)


def test_a_tampered_artifact_cannot_open_a_local_file(base_url, tmp_path):
    import asyncio
    from interface.artifact.schema import Step
    from interface.replay.executor import Replayer
    from interface.replay.result import Failed
    from tests.conftest import ENV
    from tests.test_handoff import artifact_without
    artifact = artifact_without(base_url)
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the model")
    peek = Step(id="peek", intent="open a local file", action="navigate", value=secret.as_uri(), risk="read")
    tampered = artifact.model_copy(update={"steps": [peek, *artifact.steps]})
    result = asyncio.run(Replayer(tampered, base_url, {"memberId": "12345"}, env=ENV, pending_dir=tmp_path).run())
    assert isinstance(result, Failed) and result.error_class == "POLICY_VIOLATION" and result.at_step == "peek"
