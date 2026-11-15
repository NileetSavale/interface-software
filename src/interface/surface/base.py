"""The seam between "how we perceive and act on a surface" and "the recorded flow" (docs/ARCHITECTURE.md §1-2).

Discovery, replay and the recorder only ever talk to a Surface. The web surface (Playwright) implements it over the
browser's accessibility tree; a desktop surface would implement the same methods over UIA/AX, the way agent-desktop
does. Artifacts never contain anything a Surface can't resolve from a Target.

Observations are serialisable (they go to the model and, redacted, into evidence). Element handles are not: they're
surface-specific and live only as long as the page they came from.
"""
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from interface.artifact.schema import Action, Model, Target, Visible

ErrorCode = Literal[
    "STALE_REF",           # the ref belongs to an older snapshot; observe again
    "AMBIGUOUS_TARGET",    # more than one control matched
    "TARGET_UNRESOLVED",   # nothing matched
    "NOT_ACTIONABLE",      # found, but hidden, disabled or covered (e.g. by a banner)
    "TIMEOUT",
    "NAVIGATION_BLOCKED",  # outside the allowlist
]


class AXNode(Model):
    """One node of the accessibility tree. ref is scoped to its snapshot ("s4:e12") and goes stale after it."""
    ref: str | None = Field(default=None, description="Present on nodes the model can act on.")
    role: str
    name: str = ""
    value: str | None = None
    states: list[str] = Field(default_factory=list, description="disabled, checked, expanded, focused…")
    frame: list[str] = Field(default_factory=list)
    children: list["AXNode"] = Field(default_factory=list)


class Observation(Model):
    snapshot_id: str
    url: str
    text: str = Field(description="The tree as the model reads it: one line per node, refs inline.")
    tree: AXNode | None = Field(default=None, description="Parsed form, for surfaces that build one.")
    fingerprint: str = Field(description="Hash of the screen's structure and static text, ignoring data (§7a).")
    screenshot_path: str | None = None


@dataclass
class Resolved:
    handle: object
    rank: int
    strategy: str


@dataclass
class Unresolved:
    """Why each strategy failed, in rank order, so a TARGET_UNRESOLVED failure can say what was tried."""
    attempts: list[tuple[str, ErrorCode]] = field(default_factory=list)


class SurfaceError(Exception):
    def __init__(self, code: ErrorCode, message: str, retry: Literal["safe", "never"] = "never"):
        super().__init__(message)
        self.code = code
        self.retry = retry  # from agent-desktop: only "safe" errors may be retried without re-checking the screen


class Surface(ABC):
    @abstractmethod
    async def observe(self, *, screenshot: bool = False) -> Observation:
        """The current screen as a tree with fresh refs (and a screenshot when asked)."""

    @abstractmethod
    async def describe(self, ref: str, purpose: Literal["act", "read"] = "act") -> Target:
        """The recorder's half: turn a live ref into ranked, surface-neutral strategies, each checked to find exactly
        this element. "act" describes a control by what it is; "read" describes a value by where it sits, since the
        value itself changes per record."""

    @abstractmethod
    async def resolve(self, target: Target) -> Resolved | Unresolved:
        """Replay's half: try the strategies in rank order. A strategy that matches more than one control is skipped
        as AMBIGUOUS_TARGET, never resolved by picking one."""

    @abstractmethod
    async def act(self, handle: object, action: Action, value: str | None = None,
                  guard: Callable[[], None] | None = None) -> None:
        """Perform one action. Raises SurfaceError. guard is called at the last moment before the input is
        dispatched and raises to drop it: that's where the Session's control-token epoch is checked."""

    @abstractmethod
    async def act_on_ref(self, ref: str, action: Action, value: str | None = None,
                         guard: Callable[[], None] | None = None) -> None:
        """Discovery's path: act on a ref from the latest observation. Raises SurfaceError(STALE_REF) for old refs."""

    @abstractmethod
    async def read_text(self, handle: object) -> str: ...

    @abstractmethod
    async def holds(self, condition: Visible) -> bool:
        """Whether a condition holds right now, without waiting."""

    @abstractmethod
    async def wait_any(self, conditions: list[Visible], timeout_ms: int, frame: list[str] | None = None,
                       load_state: str | None = None) -> int | None:
        """Wait until one condition holds and return its index, or None on timeout."""

    @abstractmethod
    async def navigate(self, url: str) -> None: ...

    @abstractmethod
    async def screenshot(self, path: str) -> str | None:
        """Evidence screenshot with PII masked; None if it couldn't be masked (fail closed)."""
