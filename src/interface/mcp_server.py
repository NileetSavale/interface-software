"""Approved artifacts as MCP tools: the calling agent's view of this layer.

    interface mcp --base-url http://127.0.0.1:4100              (stdio; one server per bank)
    interface mcp --base-url http://127.0.0.1:4101 --tenant b

Each approved artifact whose allowlist holds the bank's origin is one tool. Its input schema is the artifact's
inputs, its description says what it returns and which business outcomes it can answer with, and a call is a replay:
no model, the same executor, policy, redaction, evidence and circuit breaker as `interface replay`. The answer is
the result contract (success, business_outcome, failed, invalid_input) as structured content, so an agent branches
on `status` and `code` and never on prose.

What a tool is is read from disk on every list and every call: an artifact that's deprecated, put back to draft or
deleted stops being callable at once, without restarting the server. A draft is never offered.
"""
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import mcp_types as types
from mcp.server.lowlevel import Server

from interface.artifact.io import load_artifact
from interface.artifact.schema import Artifact
from interface.serverlog import ServerLog

_TYPES = {"integer": "integer"}  # everything else travels as a string; replay checks its pattern
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": ["success", "business_outcome", "failed", "invalid_input"]},
        "runId": {"type": "string", "description": "The run's id: its evidence is under runs/<runId>/."},
        "outputs": {"type": "object", "description": "On success: the values the capability read."},
        "code": {"type": "string", "description": "On business_outcome: which answer the app gave."},
        "errorClass": {"type": "string", "description": "On failed: why (TARGET_UNRESOLVED, WRONG_RECORD, ...)."},
        "atStep": {"type": "string", "description": "On business_outcome or failed: the step it happened at."},
        "errors": {"type": "array", "description": "On invalid_input: which input broke which rule."},
    },
    "required": ["status", "runId"],
}


def tool_name(artifact: Artifact) -> str:
    return artifact.capability.id.replace(".", "__")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class Catalog:
    """The approved artifacts one bank's tools come from."""

    def __init__(self, directory: Path, base_url: str, log: ServerLog | None = None):
        self.directory, self.base_url = directory, base_url.rstrip("/")
        self.log = log
        self._skipped: dict[str, str] = {}  # file -> why, so each is logged once, not on every list

    def artifacts(self) -> dict[str, Artifact]:
        found = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                artifact = load_artifact(path)
            except Exception as e:  # not an artifact, or one that no longer validates: not a tool
                why = f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
                if self.log and self._skipped.get(path.name) != why:
                    self.log.error("artifact_skipped", file=path.name, why=why)
                self._skipped[path.name] = why
                continue
            self._skipped.pop(path.name, None)
            if artifact.status == "approved" and _origin(self.base_url) in artifact.policy.allowed_origins:
                found[tool_name(artifact)] = artifact
        return found


def describe(artifact: Artifact) -> types.Tool:
    business = [o for o in artifact.outcomes if o.kind == "business"]
    reads = artifact.policy.max_risk == "read"
    text = "\n".join([
        f"{artifact.capability.title}. {artifact.capability.description}",
        f"Returns on success: {', '.join(artifact.outputs) or 'nothing (it reaches a screen)'}.",
        *(["May instead answer business_outcome, with code: " + "; ".join(
            f"{o.code} ({o.description})" if o.description else o.code for o in business)] if business else []),
        "Reads only; changes nothing." if reads else
        "Fills in a change and stops at the app's review screen: it never commits (a person does).",
        "Replays a recorded, approved procedure with no model; the same inputs take the same steps.",
    ])
    schema = {"type": "object", "additionalProperties": False, "required": list(artifact.inputs), "properties": {
        name: {"type": _TYPES.get(spec.type, "string"), "description": spec.description
               + (" Sensitive: masked in every log." if spec.sensitive else ""),
               **({"pattern": spec.pattern} if spec.pattern else {}),
               **({"enum": spec.values} if spec.values else {})}
        for name, spec in artifact.inputs.items()}}
    return types.Tool(
        name=tool_name(artifact), title=artifact.capability.title, description=text, input_schema=schema,
        output_schema=OUTPUT_SCHEMA,
        annotations=types.ToolAnnotations(title=artifact.capability.title, read_only_hint=reads,
                                          destructive_hint=False, idempotent_hint=reads, open_world_hint=False),
        _meta={"interface/capability": f"{artifact.capability.id}@{artifact.capability.version}"})


def answer(result) -> types.CallToolResult:
    """The result contract for the caller: outputs in full (it's their answer), evidence pointers left on disk."""
    body = result.model_dump(by_alias=True, exclude_none=True, include={
        "status", "run_id", "outputs", "code", "error_class", "outcome_code", "at_step", "expected", "observed",
        "errors"})
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(body))], structured_content=body,
                                is_error=result.status == "failed")


def build(directory: Path, base_url: str, *, tenant: str | None = None, env: dict[str, str] | None = None) -> Server:
    """env: where credentials come from (the process environment unless given)."""
    log = ServerLog("mcp")
    catalog = Catalog(directory, base_url, log)

    async def run(artifact, inputs):
        from interface.replay.executor import Replayer
        # Drift on a tenant proposes its override where `interface override` looks for it
        pending = {"pending_dir": directory / artifact.capability.id / "overrides.pending"} if tenant else {}
        return await Replayer(artifact, catalog.base_url, inputs, tenant=tenant, env=env, **pending).run()

    async def list_tools(ctx, params) -> types.ListToolsResult:
        tools = catalog.artifacts()
        log.event("tools_listed", tools=sorted(tools))
        return types.ListToolsResult(tools=[describe(a) for a in tools.values()])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        artifact = catalog.artifacts().get(params.name)
        if artifact is None:
            log.error("unknown_tool", tool=params.name)
            return types.CallToolResult(is_error=True, content=[types.TextContent(
                text=f"no approved capability {params.name!r} for {catalog.base_url}")])
        inputs = {k: str(v) for k, v in (params.arguments or {}).items()}
        started = time.monotonic()
        # Input names only: the values are the caller's, and the run log holds them masked
        log.event("call_started", tool=params.name, inputs=sorted(inputs), tenant=tenant)
        result = await run(artifact, inputs)
        (log.error if result.status == "failed" else log.event)(
            "call_finished", tool=params.name, run_id=result.run_id, status=result.status,
            code=getattr(result, "code", None) or getattr(result, "error_class", None),
            seconds=round(time.monotonic() - started, 2))
        return answer(result)

    return Server("interface", version="0.1.0", instructions=(
        "Each tool is a recorded, human-approved procedure in this bank's back-office app, replayed without a model. "
        "Branch on the result's status: success (outputs), business_outcome (code: an answer, not an error), failed "
        "(errorClass), invalid_input. Nothing here commits a change."),
        on_list_tools=list_tools, on_call_tool=call_tool)


async def serve(directory: Path, base_url: str, tenant: str | None = None):
    from mcp.server.stdio import stdio_server
    server = build(directory, base_url, tenant=tenant)
    log = ServerLog("mcp").echo()
    log.event("started", artifacts=str(directory), base_url=base_url, tenant=tenant,
              tools=sorted(Catalog(directory, base_url).artifacts()))
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    except Exception as e:
        log.error("crashed", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        log.event("stopped")
