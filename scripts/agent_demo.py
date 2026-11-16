"""Claude as the calling agent: answers a teller's questions using only the MCP tools (evidence/agent-mcp/).

    uv run python scripts/agent_demo.py     # needs ANTHROPIC_API_KEY; about $0.02

The other half of the thread. Discovery used Claude to learn a procedure; here Claude only calls the approved
procedures, through `interface mcp`, against a private copy of the mock app. Every tool call is a replay with no
model. The transcript is written with every value the tools returned, and every input, replaced by a placeholder,
then scanned for the mock's PII like the rest of the evidence.
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
from mcp import Client  # noqa: E402

from evals.common import ARTIFACTS, ENV, artifact, mock_app  # noqa: E402
from interface.artifact.io import dump_artifact  # noqa: E402
from interface.cli import load_dotenv  # noqa: E402
from interface.evidence import RUNS  # noqa: E402
from interface.mcp_server import build  # noqa: E402
from interface.pii import Masker  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from make_evidence import FORBIDDEN  # noqa: E402

OUT = ROOT / "evidence" / "agent-mcp"
MODEL = "claude-sonnet-5-5"
QUESTION = ("A member on the phone gives member number 23456 and asks for their savings balance. A second caller "
            "gives member number 88888. What do I tell each of them?")
SYSTEM = ("You help a credit-union teller. Use the tools to look things up in the core banking system; never guess a "
          "value. Answer in two or three short sentences.")


async def converse(client: Client) -> tuple[list[dict], dict]:
    tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
             for t in (await client.list_tools()).tools]
    claude = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": QUESTION}]
    transcript, masks, usage = [{"role": "user", "text": QUESTION}], {}, {"input": 0, "output": 0}
    for _ in range(8):
        reply = await claude.messages.create(model=MODEL, max_tokens=1024, system=SYSTEM, tools=tools,
                                             messages=messages)
        usage["input"] += reply.usage.input_tokens
        usage["output"] += reply.usage.output_tokens
        messages.append({"role": "assistant", "content": reply.content})
        results = []
        for block in reply.content:
            if block.type == "text" and block.text.strip():
                transcript.append({"role": "claude", "text": block.text})
            elif block.type == "tool_use":
                called = await client.call_tool(block.name, block.input)
                body = called.structured_content or {}
                for name, value in block.input.items():
                    masks[str(value)] = f"{{{{inputs.{name}}}}}"
                for name, value in (body.get("outputs") or {}).items():
                    masks[str(value)] = f"{{{{outputs.{name}}}}}"
                transcript.append({"role": "tool_call", "tool": block.name, "input": block.input,
                                   "result": body, "isError": called.is_error})
                results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": bool(called.is_error),
                                "content": called.content[0].text})
        if reply.stop_reason != "tool_use":
            break
        messages.append({"role": "user", "content": results})
    return transcript, {"masks": masks, "usage": usage}


def masked(text: str, masks: dict[str, str]) -> str:
    """The values the tools returned and the inputs, longest first, then the PII detectors over the rest."""
    for value, label in sorted(masks.items(), key=lambda p: -len(p[0])):
        if value:
            text = text.replace(value, label)
    return Masker().text(text)


async def main():
    load_dotenv()
    with mock_app() as app:
        catalog = Path(tempfile.mkdtemp())
        for flow, path in ARTIFACTS.items():
            (catalog / path.name).write_text(dump_artifact(artifact(flow, app)))
        async with Client(build(catalog, app.base_url, env=ENV)) as client:
            transcript, meta = await converse(client)
    # A balance may come back reformatted ("$2,500.00"): mask the digits-only form too
    masks = {**meta["masks"], **{v.replace(",", ""): k for v, k in meta["masks"].items()}}
    OUT.mkdir(parents=True, exist_ok=True)
    text = masked(json.dumps({"model": MODEL, "system": SYSTEM, "transcript": transcript, "tokens": meta["usage"]},
                             indent=2), masks)
    (OUT / "transcript.json").write_text(text + "\n")
    calls = [t for t in transcript if t["role"] == "tool_call"]
    for call in calls:  # each call's replay log, already redacted on its way to disk
        shutil.copytree(RUNS / call["result"]["runId"], OUT / "runs" / call["result"]["runId"], dirs_exist_ok=True)
    answer = next((t["text"] for t in reversed(transcript) if t["role"] == "claude"), "")
    (OUT / "README.md").write_text(masked(f"""# Claude as the calling agent, over MCP

Generated by `uv run python scripts/agent_demo.py`. Claude ({MODEL}) was given only the tools `interface mcp`
serves for the mock bank (each an approved artifact) and a teller's question. Every tool call below was a replay with
no model; each one's replay log is under `runs/`. Inputs and returned values are placeholders here; Claude saw them in full.

**Question:** {QUESTION}

| Tool | Input | Result | Run |
|---|---|---|---|
""" + "\n".join(f"| `{c['tool']}` | `{json.dumps(c['input'])}` | `{c['result'].get('status')}"
                + (f"/{c['result']['code']}" if c['result'].get('code') else "") + f"` | `{c['result'].get('runId')}` |"
                for c in calls) + f"""

**Claude's answer:** {answer}

Tokens: {meta['usage']['input']} in, {meta['usage']['output']} out. The full exchange is in
[`transcript.json`](transcript.json).
""", masks) + "\n")
    leaks = [f"{p.name}: {v}" for p in OUT.rglob("*") if p.suffix in {".json", ".jsonl", ".md", ".txt"}
             for v in FORBIDDEN if v in p.read_text()]
    if leaks:
        sys.exit("LEAKS:\n" + "\n".join(leaks))
    print(f"{len(calls)} tool calls; answer: {masked(answer, masks)}; scan clean")


if __name__ == "__main__":
    asyncio.run(main())
