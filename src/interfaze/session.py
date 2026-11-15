"""Who may act on a live browser session right now (docs/ARCHITECTURE.md §5, handoff package).

    AUTOMATION ──pause──► PAUSED_FOR_HUMAN ──claim──► HUMAN ──hand back──► RESUMING ──verified──► AUTOMATION
                                 ▲                       │ (token expires)        │ (check fails)
                                 └───────────────────────┴────────────────────────┘
    any state ──abort──► ABORTED

A state check before an action isn't enough: Playwright actions are async, so automation can issue a click, control
can pass to a person, and the click can land afterwards. So control is a token with an epoch that goes up on every
transition. Automation captures the epoch when it decides to act and the surface checks it again at the last moment
before the input is dispatched; an action from an older epoch is dropped.
"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Literal

from interfaze.evidence import RunLog

State = Literal["automation", "paused_for_human", "human", "resuming", "aborted"]
AUTOMATION = "automation"


class ControlLost(Exception):
    """Automation tried to act without holding control, or with an epoch from before a handoff."""


class ControlError(Exception):
    """An operator request that the current state doesn't allow (hand back without holding control, say)."""


@dataclass
class ControlToken:
    holder: str            # "automation", "operator:<name>", or "nobody" while paused
    epoch: int
    expires_at: float | None = None


@dataclass
class Intervention:
    id: str
    run_id: str
    subject: str           # capability or discovery goal
    step_id: str
    intent: str
    reason: str
    since: str
    snapshot: str          # redacted
    screenshot: str | None = None
    human_actions: list[dict] = field(default_factory=list)


class Session:
    def __init__(self, log: RunLog, operator_ttl_s: float = 300):
        self.log = log
        self.operator_ttl_s = operator_ttl_s
        self.token = ControlToken(holder=AUTOMATION, epoch=0)
        self.state: State = "automation"
        self.intervention: Intervention | None = None
        self._changed = asyncio.Event()

    @property
    def epoch(self) -> int:
        return self.token.epoch

    def check(self, epoch: int):
        """Raises unless automation holds control under the same epoch it decided to act in."""
        if self.token.holder != AUTOMATION or self.token.epoch != epoch:
            raise ControlLost(f"control is with {self.token.holder} (epoch {self.token.epoch}); an action decided in "
                              f"epoch {epoch} was dropped")

    def human_in_control(self) -> bool:
        return self.state == "human"

    def _move(self, to: State, holder: str, by: str, reason: str, ttl: float | None = None):
        before = self.state
        self.token = ControlToken(holder=holder, epoch=self.token.epoch + 1,
                                  expires_at=time.time() + ttl if ttl else None)
        self.state = to
        self.log.event("control", **{"from": before, "to": to, "epoch": self.token.epoch, "holder": holder, "by": by,
                                     "reason": reason})
        self._changed.set()

    # ── Automation's side ──────────────────────────────────────────────────────────────────────────────────────

    def pause(self, intervention: Intervention):
        self.intervention = intervention
        self._move("paused_for_human", "nobody", AUTOMATION, intervention.reason)

    def resume_verified(self):
        self._move("automation", AUTOMATION, AUTOMATION, "resume check passed")
        self.intervention = None

    def resume_rejected(self, reason: str):
        self._move("paused_for_human", "nobody", AUTOMATION, reason)
        if self.intervention:
            self.intervention.reason = reason

    async def wait_for_hand_back(self, timeout_s: float) -> Literal["resuming", "aborted", "timeout"]:
        """Blocks while a person works. An operator who goes quiet past their token's expiry loses control, and the
        run waits for someone to claim it again; nobody at all within timeout_s ends it."""
        deadline = time.monotonic() + timeout_s
        while self.state not in ("resuming", "aborted"):
            if self.state == "human" and self.token.expires_at and time.time() > self.token.expires_at:
                self._move("paused_for_human", "nobody", "system", "operator token expired")
            remaining = deadline - time.monotonic()
            if remaining <= 0 and self.state == "paused_for_human":
                return "timeout"
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=min(1.0, max(remaining, 0.05)))
            except asyncio.TimeoutError:
                pass
        return self.state

    # ── The operator's side ────────────────────────────────────────────────────────────────────────────────────

    def claim(self, operator: str):
        if self.state != "paused_for_human":
            raise ControlError(f"nothing to claim: the session is {self.state}")
        self._move("human", f"operator:{operator}", operator, "operator took control", ttl=self.operator_ttl_s)

    def heartbeat(self, operator: str):
        if self.token.holder == f"operator:{operator}":
            self.token.expires_at = time.time() + self.operator_ttl_s

    def hand_back(self, operator: str):
        if self.token.holder != f"operator:{operator}":
            raise ControlError(f"{operator} doesn't hold control (holder: {self.token.holder})")
        self._move("resuming", AUTOMATION, operator, "operator handed back")

    def abort(self, by: str, reason: str = "aborted by operator"):
        if self.state != "aborted":
            self._move("aborted", "nobody", by, reason)

    def record_human(self, action: dict):
        """An action a person took while holding control: evidence in replay, a step in discovery."""
        if self.intervention is not None:
            self.intervention.human_actions.append(action)
        self.log.event("human_action", actor=self.token.holder, at_epoch=self.token.epoch, **action)

    def summary(self) -> dict:
        return {"state": self.state, "holder": self.token.holder, "epoch": self.token.epoch}
