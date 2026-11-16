"""Approved artifacts as MCP tools, through a real MCP client in process, replaying on the mock app."""
import asyncio
import json
from pathlib import Path

from mcp import Client

from interface.artifact.io import dump_artifact, load_artifact
from interface.mcp_server import build
from tests.conftest import ENV

ROOT = Path(__file__).resolve().parent.parent


def catalog(tmp_path, base_url, approved=("lookup_member_balance", "open_sub_account"), drafts=("find_member_by_name",)):
    """Copies of the repo's artifacts, allowed on this test's mock app; the parabank ones as they are."""
    for name, status in [*((n, "approved") for n in approved), *((n, "draft") for n in drafts)]:
        artifact = load_artifact(ROOT / "artifacts" / f"core_banking.{name}.json")
        policy = artifact.policy.model_copy(update={"allowed_origins": [base_url]})
        (tmp_path / f"core_banking.{name}.json").write_text(
            dump_artifact(artifact.model_copy(update={"status": status, "policy": policy})))
    parabank = load_artifact(ROOT / "artifacts" / "parabank.account_balance.json")
    (tmp_path / "parabank.account_balance.json").write_text(
        dump_artifact(parabank.model_copy(update={"status": "approved"})))
    (tmp_path / "notes.json").write_text("{}")  # not an artifact
    return tmp_path


async def session(directory, base_url, calls):
    async with Client(build(directory, base_url, env=ENV)) as client:
        return [await call(client) for call in calls]


def test_only_this_banks_approved_artifacts_are_tools(tmp_path, base_url):
    [listed] = asyncio.run(session(catalog(tmp_path, base_url), base_url, [lambda c: c.list_tools()]))
    tools = {t.name: t for t in listed.tools}
    # Not the draft, not ParaBank's (another origin), not the stray file
    assert set(tools) == {"core_banking__lookup_member_balance", "core_banking__open_sub_account"}
    lookup = tools["core_banking__lookup_member_balance"]
    assert lookup.input_schema["required"] == ["memberId"]
    assert lookup.input_schema["properties"]["memberId"]["pattern"] == "^[0-9]{5}$"
    assert "MEMBER_NOT_FOUND" in lookup.description
    assert lookup.annotations.read_only_hint and not lookup.annotations.destructive_hint
    sub = tools["core_banking__open_sub_account"]
    assert not sub.annotations.read_only_hint and "never commits" in sub.description


def test_a_call_is_a_replay_with_the_result_contract(tmp_path, base_url):
    directory = catalog(tmp_path, base_url)
    tool = "core_banking__lookup_member_balance"
    found, missing, malformed = asyncio.run(session(directory, base_url, [
        lambda c: c.call_tool(tool, {"memberId": "12345"}),
        lambda c: c.call_tool(tool, {"memberId": "88888"}),
        lambda c: c.call_tool(tool, {"memberId": "1234"}),
    ]))
    assert found.structured_content["status"] == "success" and not found.is_error
    assert found.structured_content["outputs"]["memberName"] == "Jane Doe"  # the caller gets the answer in full
    assert Path("runs", found.structured_content["runId"], "events.jsonl").exists()  # its evidence, as any replay
    # An answer, not an error
    assert missing.structured_content["status"] == "business_outcome" and not missing.is_error
    assert missing.structured_content["code"] == "MEMBER_NOT_FOUND"
    # Refused before the app is touched
    assert malformed.structured_content["status"] == "invalid_input"


def test_a_capability_withdrawn_after_listing_is_refused(tmp_path, base_url):
    directory = catalog(tmp_path, base_url)
    tool = "core_banking__lookup_member_balance"

    async def withdraw(client):
        path = directory / "core_banking.lookup_member_balance.json"
        path.write_text(dump_artifact(load_artifact(path).model_copy(update={"status": "deprecated"})))
        return await client.call_tool(tool, {"memberId": "12345"})

    listed, refused = asyncio.run(session(directory, base_url, [lambda c: c.list_tools(), withdraw]))
    assert tool in {t.name for t in listed.tools}
    assert refused.is_error and "no approved capability" in refused.content[0].text


def test_the_server_log_says_what_was_skipped_and_how_each_call_ended(tmp_path, base_url):
    log = Path("runs", "mcp.log")
    before = log.read_text().count("\n") if log.exists() else 0
    directory = catalog(tmp_path, base_url)
    tool = "core_banking__lookup_member_balance"
    asyncio.run(session(directory, base_url, [lambda c: c.list_tools(),
                                              lambda c: c.call_tool(tool, {"memberId": "12345"}),
                                              lambda c: c.call_tool("core_banking__nope", {})]))
    lines = log.read_text().splitlines()[before:]
    events = [json.loads(line) for line in lines]
    skipped = next(e for e in events if e["event"] == "artifact_skipped")
    assert skipped["file"] == "notes.json" and skipped["why"]  # once, with the reason, no longer silent
    finished = next(e for e in events if e["event"] == "call_finished")
    assert finished["tool"] == tool and finished["status"] == "success" and finished["run_id"].startswith("replay_")
    assert any(e["event"] == "unknown_tool" for e in events)
    assert "12345" not in "\n".join(lines) and "Jane Doe" not in "\n".join(lines)  # names of inputs, never values
