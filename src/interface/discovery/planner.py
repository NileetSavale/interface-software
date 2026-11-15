"""Who decides the next action during discovery. Claude in production; a script in tests.

The history the planner is given is append-only. The current models bind their thinking to the exact conversation
that produced it, so editing an earlier message (say, shortening an old snapshot) would invalidate it. Old snapshots
are cleared server-side instead, through context editing, which doesn't count as an edit (D17).
"""
from dataclasses import dataclass, field
from typing import Protocol

import anthropic

CONTEXT_EDITING_BETA = "context-management-2025-06-27"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Old snapshots describe screens that no longer exist and their refs are dead. Once a request passes the trigger, the
# API clears all but the last two tool results. Same mechanism as loop_runner's CONTEXT_EDITING.
CONTEXT_EDITING = {"edits": [{
    "type": "clear_tool_uses_20250919",
    "trigger": {"type": "input_tokens", "value": 12_000},
    "keep": {"type": "tool_uses", "value": 2},
    "clear_at_least": {"type": "input_tokens", "value": 3_000},
}]}
# USD per million tokens: input, output, cache read, 5-minute cache write
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost(self, model: str) -> float:
        i, o, r, w = PRICES.get(model, PRICES["claude-opus-5-5"])
        return (self.input_tokens * i + self.output_tokens * o + self.cache_read_tokens * r
                + self.cache_write_tokens * w) / 1_000_000


@dataclass
class Turn:
    content: list            # the assistant message's blocks, to be sent back unchanged
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def tool_calls(self) -> list[dict]:
        return [{"id": _get(b, "id"), "name": _get(b, "name"), "input": _get(b, "input")}
                for b in self.content if _get(b, "type") == "tool_use"]

    def text(self) -> str:
        return "\n".join(_get(b, "text") for b in self.content if _get(b, "type") == "text").strip()


def _get(block, key):
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


class Planner(Protocol):
    model: str

    async def turn(self, system: str, tools: list[dict], messages: list) -> Turn: ...


class ClaudePlanner:
    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "medium"):
        self.model = model
        self.effort = effort
        self.client = anthropic.AsyncAnthropic()

    async def turn(self, system: str, tools: list[dict], messages: list) -> Turn:
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=16_000,
            system=system,
            tools=tools,
            messages=messages,
            cache_control={"type": "ephemeral"},  # the whole history is resent each step; cache it
            output_config={"effort": self.effort},
            context_management=CONTEXT_EDITING,
            fallbacks="default",                  # on a safety decline, the API re-runs on a fallback model
            betas=[CONTEXT_EDITING_BETA, FALLBACK_BETA],
        )
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_input_tokens or 0,
            cache_write_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        return Turn(content=_echoable(response.content), stop_reason=response.stop_reason, usage=usage,
                    model=response.model)

    async def close(self):
        await self.client.close()  # inside the event loop, or its connections are torn down after the loop has closed


def _echoable(content: list) -> list:
    """After a mid-output fallback, the blocks before the last fallback marker belong to the declined attempt:
    only their text may be sent back."""
    last = max((i for i, b in enumerate(content) if _get(b, "type") == "fallback"), default=None)
    if last is None:
        return list(content)
    return [b for b in content[:last] if _get(b, "type") == "text"] + list(content[last + 1:])


class ScriptedPlanner:
    """Plays back a fixed list of tool calls: tests the discovery loop and recorder without a model."""

    def __init__(self, calls: list[tuple[str, dict]], model: str = "scripted"):
        self.model = model
        self._calls = list(calls)
        self.seen: list[str] = []  # the tool results and notes it was shown, for assertions

    async def turn(self, system: str, tools: list[dict], messages: list) -> Turn:
        last = messages[-1]["content"]
        if isinstance(last, list):
            self.seen += [b["content"] if b.get("type") == "tool_result" else b["text"]
                          for b in last if isinstance(b, dict) and b.get("type") in ("tool_result", "text")]
        elif len(messages) == 1:
            self.seen.append(last)
        if not self._calls:
            return Turn(content=[{"type": "text", "text": "out of script"}], stop_reason="end_turn")
        name, args = self._calls.pop(0)
        if callable(args):
            # A single-message conversation (a probe) shows its screen in that message; otherwise it's the latest
            history = [_first_text(messages)] if len(messages) == 1 else [_first_text(messages), *self.seen]
            screens = [s for s in history if "Current screen:" in s]
            args = args(screens[-1])
        return Turn(content=[{"type": "tool_use", "id": f"call_{len(self.seen)}", "name": name, "input": args}],
                    stop_reason="tool_use")


def _first_text(messages: list) -> str:
    content = messages[0]["content"]
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict))
