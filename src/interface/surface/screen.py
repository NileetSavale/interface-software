"""What the terminal and vision surfaces share: a screen read as a flat list of positioned elements, with no
accessibility tree under it. Each subclass says how it reads the screen (_parse), how it knows the screen has
stopped changing (_quiet), and how it presses a button and fills a field; everything else is here, once:

- the snapshot the model reads, in the web surface's format, refs inline;
- the recorder's ranked strategies: role and name, label, the text it's right of, and last its place (bbox), each
  kept only if it finds exactly this element;
- replay's resolve in rank order, where more than one hit is AMBIGUOUS_TARGET, never a pick;
- conditions (holds, wait_any) and the commit guard: on a screen the policy lists in commitPaths, no button that
  submits is ever pressed, whatever it's called; only exit and cancel get through.

Every target is scoped to its screen code (frame): nothing is typed into a field on the wrong screen.
"""
import asyncio
import fnmatch
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from interface.artifact.schema import (
    Action, AnchorStrategy, BBoxStrategy, LabelStrategy, Policy, Regex, RoleStrategy, Strategy, Target, TextMatch,
    Visible,
)
from interface.surface.base import ErrorCode, Observation, Resolved, Surface, SurfaceError, Unresolved

_CODE = re.compile(r"^[A-Z]{2,4}\d{2,5}$")
_LEADER = re.compile(r"(?:\s*\.)*\s*:$")
_SAFE_KEY = re.compile(r"(?i)\b(exit|cancel|back|previous|help|sign off)\b")
POLL_S = 0.1


@dataclass(frozen=True)
class Element:
    role: Literal["heading", "textbox", "button", "link", "text"]
    name: str  # what it's called: a field's label, a key's caption, a text's words
    row: int  # the line it's on: a terminal row, or a line of text found in a picture
    col: int  # where it starts: a column, or a pixel
    width: int
    value: str = ""  # a field's contents
    key: str = ""  # a terminal button's key
    screen: str = ""
    box: tuple[int, int, int, int] | None = None  # in pixels (x, y, w, h), on a surface read from pixels
    hidden: bool = False  # a field that shows a mask, not what was typed
    mask_count: int = 0  # how many mask characters it shows, when they could be counted (0: not known)

    @property
    def text(self) -> str:
        return self.value if self.role == "textbox" else self.name


def _matches(match: TextMatch, text: str) -> bool:
    if isinstance(match, Regex):
        return re.search(match.matches, text) is not None
    return match.strip() == text.strip()


def label_of(text: str) -> str:
    """A caption without its dot leaders: "Member # . . . :" → "Member #:"."""
    return _LEADER.sub(":", text.strip()) if _LEADER.search(text) else text.strip()


class ScreenSurface(Surface):
    clickable: tuple[str, ...] = ("button", "link")

    def __init__(self, policy: Policy | None = None):
        self.policy = policy
        self.origin = ""
        self._elements: list[Element] = []
        self._refs: dict[str, Element] = {}
        self._snapshot = ""
        self._snapshots = 0
        self.blocked: list[str] = []

    def refusals_since(self, mark: int) -> list[str]:
        return self.blocked[mark:]

    # ── What a subclass provides ───────────────────────────────────────────────────────────────────────────────

    def _parse(self) -> list[Element]:
        raise NotImplementedError

    async def _quiet(self, wait_s: float = 2.0):
        """Until the screen has stopped changing: it's read whole, never half-drawn."""
        raise NotImplementedError

    async def _press(self, button: Element):
        raise NotImplementedError

    async def _type(self, field: Element, value: str, guard: Callable[[], None] | None):
        raise NotImplementedError

    def _match(self, match: TextMatch, text: str) -> bool:
        return _matches(match, text)

    def _bbox(self, e: Element) -> BBoxStrategy:
        return BBoxStrategy(x=e.col, y=e.row, width=e.width, height=1)

    def _at(self, elements: list[Element], x: float, y: float, w: float, h: float) -> list[Element]:
        return [e for e in elements if e.row == int(y) and e.col <= int(x) < e.col + max(e.width, 1)
                and e.role != "heading"]

    def _same(self, a: Element, b: Element) -> bool:
        """The same element, as the screen is now: same place, same screen."""
        return (a.row, a.col, a.screen) == (b.row, b.col, b.screen)

    @property
    def code(self) -> str:
        return next((e.screen for e in self._parse()[:1]), "")

    async def observe(self, *, screenshot: bool = False, screenshot_path: str | None = None) -> Observation:
        from interface.surface.web import fingerprint
        await self._quiet()
        self._elements = self._parse()
        self._snapshots += 1
        # A caption right in front of a field is that field's name, as a <label> is on the web: listed once, as
        # the textbox, so it's never read as a label for a value
        shown = [e for i, e in enumerate(self._elements)
                 if not (e.role == "text" and i + 1 < len(self._elements) and self._elements[i + 1].role == "textbox"
                         and self._elements[i + 1].row == e.row and self._elements[i + 1].name == e.name.rstrip(":"))]
        self._refs = {f"e{i}": e for i, e in enumerate(shown, 1)}
        lines = []
        for ref, e in self._refs.items():
            line = f'- {e.role} "{e.name}"' + (" [level=1]" if e.role == "heading" else "") + f" [ref={ref}]"
            if e.role == "textbox":
                line += f": {e.value}" if e.value else ""
            elif e.role == "button" and e.key:
                line += f" (key {e.key})"
            lines.append(line)
        self._snapshot = "\n".join(lines)
        code = self._elements[0].screen if self._elements else ""
        return Observation(snapshot_id=f"s{self._snapshots}", url=f"{self.origin}/{code}", text=self._snapshot,
                           fingerprint=fingerprint(self._snapshot))


    # ── Discovery: refs from the latest observation ────────────────────────────────────────────────────────────

    def _element(self, ref: str) -> Element:
        element = self._refs.get(ref)
        if element is None:
            raise SurfaceError("STALE_REF", f"ref {ref} isn't in the latest snapshot", retry="safe")
        return element

    def ref_line(self, ref: str) -> tuple[str, str]:
        e = self._element(ref)
        return e.role, e.name

    async def act_on_ref(self, ref: str, action: Action, value: str | None = None,
                         guard: Callable[[], None] | None = None) -> None:
        await self.act(self._element(ref), action, value, guard)

    async def read_ref(self, ref: str) -> str:
        return await self.read_text(self._element(ref))

    async def frame_of(self, ref: str) -> list[str]:
        return [self._element(ref).screen]

    async def form_of(self, ref: str) -> str | None:
        return None  # no forms: a screen is one form, scoped by its code

    async def path_of(self, ref: str) -> str:
        return f"/{self._element(ref).screen}"

    async def describe(self, ref: str, purpose: Literal["act", "read"] = "act",
                       inputs: dict[str, str] | None = None, first_result: bool = False) -> Target:
        e = self._element(ref)
        before = next((x for x in reversed(self._elements) if x.row == e.row and x.col < e.col and x.role == "text"),
                      None)
        candidates: list[Strategy] = []
        if purpose == "act" and e.name and not re.search(r"\d", e.name):
            candidates.append(RoleStrategy(role=e.role, name=e.name))
            if e.role == "textbox":
                candidates.append(LabelStrategy(label=e.name))
        if before and before.name.endswith(":"):
            candidates.append(AnchorStrategy(anchor_text=before.name, relation="right-of",
                                             role=e.role if purpose == "act" else None))
        if e.screen:  # a place means something only on a known screen: without a screen code it lands on any page
            candidates.append(self._bbox(e))
        # What an input's value identifies is found by that value, never by its place: in a list, the place is
        # someone else's row on the next run (ParaBank's accounts, read off pixels). An element showing an input is
        # `role "{{inputs.x}}"`; a value on a row that starts with one is right of it. A label anchor, where there
        # is one, stays first for an identity check, so a detail page showing another record is still WRONG_RECORD
        shows = lambda x: next((k for k, v in (inputs or {}).items() if v and x.name.strip() == v.strip()), None)  # noqa: E731
        owner = next((x for x in reversed(self._elements) if x.row == e.row and x.col < e.col and shows(x)), None)
        labelled = any(isinstance(c, AnchorStrategy) for c in candidates)
        if (given := shows(e)) and (purpose == "act" or not labelled):
            candidates = [RoleStrategy(role=e.role, name=f"{{{{inputs.{given}}}}}")]
        elif purpose == "read" and owner and not labelled:
            candidates = [AnchorStrategy(anchor_text=f"{{{{inputs.{shows(owner)}}}}}", relation="right-of")]
        target = Target(frame=[e.screen], strategies=candidates)

        def found(strategy: Strategy) -> list[Element]:  # with this run's values in place of the placeholders
            bound = strategy.model_copy(update={k: re.sub(r"\{\{inputs\.(\w+)\}\}", lambda m: (inputs or {})[m[1]], v)
                                               for k in ("name", "anchor_text") if isinstance(v := getattr(strategy, k, None), str)})
            return self._hits(target, bound, purpose)
        kept = [s for s in candidates if [(h.row, h.col) for h in found(s)] == [(e.row, e.col)]]
        if not kept:
            raise SurfaceError("TARGET_UNRESOLVED", f"no stable way to find {e.role} {e.name!r} again")
        return target.model_copy(update={"strategies": kept})

    # ── Replay: resolving recorded targets ─────────────────────────────────────────────────────────────────────

    def _on_screen(self, frame: list[str] | None) -> list[Element]:
        elements = self._parse()
        code = elements[0].screen if elements else ""
        return elements if not frame or frame == [code] else []

    def _hits(self, target: Target, strategy: Strategy, purpose: str = "") -> list[Element]:
        elements = self._on_screen(target.frame)
        match strategy:
            case RoleStrategy(role=role, name=name):
                return [e for e in elements if e.role == role and self._match(name, e.name)]
            case LabelStrategy(label=label):
                return [e for e in elements if e.role == "textbox" and self._match(label, e.name)]
            case AnchorStrategy(anchor_text=anchor, relation=relation, role=role) if relation in ("right-of", "after"):
                hits = []
                for i, a in enumerate(elements):
                    if a.role in ("text", "link") and self._match(anchor, a.name):
                        nxt = next((e for e in elements[i + 1:] if (relation == "after" or e.row == a.row)
                                    and (role is None or e.role == role)), None)
                        if nxt and (relation == "after" or nxt.row == a.row):
                            hits.append(nxt)
                return hits
            case BBoxStrategy(x=x, y=y, width=w, height=h):
                return self._at(elements, x, y, w, h)
        return []

    async def resolve(self, target: Target, timeout_ms: int = 10_000) -> Resolved | Unresolved:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            await self._quiet()
            attempts: list[tuple[str, ErrorCode]] = []
            for rank, strategy in enumerate(target.strategies):
                hits = self._hits(target, strategy)
                # An empty value isn't there yet (a screen that fills in after it's drawn)
                hits = [h for h in hits if h.role != "text" or h.name]
                if len(hits) == 1:
                    return Resolved(handle=hits[0], rank=rank, strategy=strategy.kind)
                attempts.append((strategy.kind, "AMBIGUOUS_TARGET" if hits else "TARGET_UNRESOLVED"))
            if time.monotonic() >= deadline:
                return Unresolved(attempts=attempts)
            await asyncio.sleep(POLL_S)

    def _live(self, handle: object) -> Element:
        """The element as it is now, at the same place on the same screen; the screen may have moved on."""
        assert isinstance(handle, Element)
        for e in self._parse():
            if e.role == handle.role and self._same(e, handle):
                return e
        raise SurfaceError("NOT_ACTIONABLE", f"{handle.role} {handle.name!r} is no longer on the screen", retry="safe")

    # ── Acting ─────────────────────────────────────────────────────────────────────────────────────────────────

    async def act(self, handle: object, action: Action, value: str | None = None,
                  guard: Callable[[], None] | None = None) -> None:
        e = self._live(handle)
        if action == "type" and e.role == "textbox":
            await self._type(e, value or "", guard)
        elif action in ("click", "press") and e.role in self.clickable:
            if guard:
                guard()
            if reason := self._refused(e):
                self.blocked.append(reason)
                return
            await self._press(e)
        else:
            raise SurfaceError("NOT_ACTIONABLE", f"{action} isn't something a {e.role} here does")

    def _refused(self, key: Element) -> str | None:
        if self.policy is None or _SAFE_KEY.search(key.name):
            return None
        path = f"/{key.screen}"
        if any(fnmatch.fnmatch(path, p) for p in self.policy.commit_paths):
            pressed = f"{key.key}={key.name}" if key.key else repr(key.name)
            return (f"blocked by policy: {pressed} on {key.screen} would commit a change (irreversible). "
                    f"Automation can reach a commit point but never press it; stop here.")
        return None

    async def read_text(self, handle: object) -> str:
        e = self._live(handle) if isinstance(handle, Element) and handle.role == "textbox" else handle
        if isinstance(e, Element) and e.role == "text":
            # Re-read the cells now: the screen may have filled in since it was resolved
            for live in self._parse():
                if live.role == "text" and self._same(live, e):
                    return live.name
            return ""
        return e.text.strip()

    # ── Conditions ─────────────────────────────────────────────────────────────────────────────────────────────

    async def holds(self, condition: Visible) -> bool:
        for e in self._on_screen(condition.frame):
            if condition.role and e.role != condition.role:
                continue
            if condition.name is not None and not self._match(condition.name, e.name):
                continue
            if condition.label is not None and not (e.role == "textbox" and self._match(condition.label, e.name)):
                continue
            if condition.text is not None and not self._match(condition.text, e.text if e.role == "textbox" else e.name):
                continue
            return True
        return False

    async def wait_any(self, conditions: list[Visible], timeout_ms: int, frame: list[str] | None = None,
                       load_state: str | None = None) -> int | None:
        deadline = time.monotonic() + timeout_ms / 1000
        if not conditions:
            return None
        while True:
            await self._quiet()
            for i, condition in enumerate(conditions):
                if await self.holds(condition):
                    return i
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(POLL_S)

    async def gone(self, handle: object, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                self._live(handle)
            except SurfaceError:
                return
            await asyncio.sleep(POLL_S)

    async def headings(self) -> list[Visible]:
        return [Visible(frame=[e.screen], role="heading", name=e.name) for e in self._parse()
                if e.role == "heading" and not re.search(r"\d", e.name)]

    # What the web surface reads off a DOM element, for the executor and the discovery loop
    async def control_name(self, handle: object) -> str:
        return self._live(handle).name

    async def live_name(self, handle: object) -> str:
        return self._live(handle).name

    async def is_link(self, handle: object) -> bool:
        return False

    async def form_key(self, handle: object) -> str | None:
        return None

    async def path_of_handle(self, handle: object) -> str:
        return f"/{self._live(handle).screen}"
