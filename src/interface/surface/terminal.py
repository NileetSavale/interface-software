"""The terminal Surface: a green-screen app read through a VT100 emulator, character by character.

The host is reached the way real ones are, over telnet-style TCP (telnet://host:port), and what it sends is fed to
pyte, a VT100 emulator, which keeps the 24x80 grid with each cell's attributes. There's no accessibility tree, so
this builds one from the characters, by the conventions green screens share:

- the first row is the screen: its code (CB0300), which also scopes every target, and its title (a heading);
- a run of reverse-video cells is a field you can type in (a textbox), named by the label in front of it, with the
  dot leaders taken out ("Member # . . . :" → "Member #");
- the last row is the function-key line ("Enter=Search  F3=Exit"): each key is a button;
- everything else is text, split where two or more spaces separate it: a label and its value are two elements.

The model reads that tree in the same format as a web snapshot, through the same masker, and acts by ref. The
recorder describes a control by role and name, label, the text it's right of, and last its place on the grid (a
bbox in character cells, 0-based), which survives a relabel because a terminal's grid doesn't move. Every target is
scoped to its screen code: nothing is typed into a field on the wrong screen.

The guard below the screen: on a screen the policy lists in commitPaths (as /CB0410), no key that submits is ever
sent, whatever it's called; only exit and cancel keys get through.
"""
import asyncio
import fnmatch
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import pyte

from interface.artifact.schema import (
    Action, AnchorStrategy, BBoxStrategy, LabelStrategy, Policy, Regex, RoleStrategy, Strategy, Target, TextMatch,
    Visible,
)
from interface.surface.base import ErrorCode, Observation, Resolved, Surface, SurfaceError, Unresolved

ROWS, COLS = 24, 80
KEY_BYTES = {"Enter": b"\r", "F1": b"\x1bOP", "F2": b"\x1bOQ", "F3": b"\x1bOR", "F4": b"\x1bOS", "F5": b"\x1b[15~",
             "F6": b"\x1b[17~", "F7": b"\x1b[18~", "F8": b"\x1b[19~", "F9": b"\x1b[20~", "F10": b"\x1b[21~",
             "F11": b"\x1b[23~", "F12": b"\x1b[24~"}
TAB, ERASE = b"\t", b"\x15"
_CODE = re.compile(r"^[A-Z]{2,4}\d{2,5}$")
_KEY = re.compile(r"(Enter|F\d{1,2})=(\S+(?: \S+)*)")
_LEADER = re.compile(r"(?:\s*\.)*\s*:$")
_SAFE_KEY = re.compile(r"(?i)\b(exit|cancel|back|previous|help|sign off)\b")
QUIET_S = 0.12
POLL_S = 0.1


@dataclass(frozen=True)
class Element:
    role: Literal["heading", "textbox", "button", "text"]
    name: str  # what it's called: a field's label, a key's caption, a text's words
    row: int
    col: int
    width: int
    value: str = ""  # a field's contents
    key: str = ""  # a button's key
    screen: str = ""

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


class TerminalSurface(Surface):
    def __init__(self, policy: Policy | None = None):
        self.policy = policy
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.origin = ""
        self._pump: asyncio.Task | None = None
        self._received = 0  # bytes received so far
        self._last = 0.0  # when the last bytes arrived
        self._elements: list[Element] = []
        self._refs: dict[str, Element] = {}
        self._snapshot = ""
        self._snapshots = 0
        self.blocked: list[str] = []

    def refusals_since(self, mark: int) -> list[str]:
        return self.blocked[mark:]

    # ── The connection ─────────────────────────────────────────────────────────────────────────────────────────

    async def navigate(self, url: str) -> None:
        """Connect (again) to the host: a fresh session at its first screen."""
        parts = urlsplit(url)
        if parts.scheme != "telnet" or not parts.port:
            raise SurfaceError("NAVIGATION_BLOCKED", f"a terminal host is telnet://host:port, not {url}")
        await self.close()
        self.origin = f"telnet://{parts.netloc}"
        self.screen.reset()
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(parts.hostname, parts.port), timeout=10)
        except (OSError, asyncio.TimeoutError) as e:
            raise SurfaceError("TIMEOUT", f"can't reach {self.origin}: {e}", retry="safe") from e
        self._received, self._last = 0, time.monotonic()
        self._pump = asyncio.create_task(self._read())
        await self._settle(0, wait_s=10)

    async def _read(self):
        while self.reader and not self.reader.at_eof():
            data = await self.reader.read(4096)
            if not data:
                break
            self.stream.feed(data)
            self._received += len(data)
            self._last = time.monotonic()

    async def close(self):
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self.writer:
            self.writer.close()
            self.writer = None

    async def _send(self, data: bytes):
        if self.writer is None or self.writer.is_closing():
            raise SurfaceError("NOT_ACTIONABLE", "the terminal session is closed")
        self.writer.write(data)
        await self.writer.drain()

    async def _settle(self, mark: int, wait_s: float = 3.0):
        """Until the host has answered (bytes past mark) and gone quiet, or wait_s passes with no answer."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self._received > mark and time.monotonic() - self._last >= QUIET_S:
                return
            await asyncio.sleep(0.02)

    async def _quiet(self, wait_s: float = 2.0):
        """Until the host has stopped sending for a moment: a screen is read whole, never half-drawn."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() - self._last < QUIET_S and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    # ── Reading the screen ─────────────────────────────────────────────────────────────────────────────────────

    def _parse(self) -> list[Element]:
        buffer = self.screen.buffer
        first = "".join(buffer[0][c].data for c in range(COLS))
        tokens = [t for t in re.split(r"\s{2,}", first.strip()) if t]
        code = tokens[0] if tokens and _CODE.match(tokens[0]) else ""
        elements: list[Element] = []
        for row in range(ROWS):
            cells = [buffer[row][c] for c in range(COLS)]
            line = "".join(c.data for c in cells)
            if row == 0:
                col = 0
                for i, token in enumerate(tokens):
                    col = line.index(token, col)
                    role = "heading" if i == 1 or (i == 0 and not code) else "text"
                    elements.append(Element(role, token, row, col, len(token), screen=code))
                    col += len(token)
                continue
            if row == ROWS - 1 and _KEY.search(line):
                for m in _KEY.finditer(line):
                    elements.append(Element("button", m.group(2), row, m.start(), m.end() - m.start(), key=m.group(1),
                                            screen=code))
                continue
            col = 0
            while col < COLS:
                if cells[col].reverse:  # a field: the run of reverse-video cells
                    end = col
                    while end < COLS and cells[end].reverse:
                        end += 1
                    before = next((e for e in reversed(elements) if e.row == row and e.role == "text"), None)
                    name = label_of(before.name).rstrip(":").strip() if before else ""
                    elements.append(Element("textbox", name, row, col, end - col, line[col:end].rstrip(), screen=code))
                    col = end
                    continue
                if line[col] == " ":
                    col += 1
                    continue
                end = col
                while end < COLS and not cells[end].reverse and line[end:end + 2] != "  " and \
                        not (line[end] == " " and end + 1 < COLS and cells[end + 1].reverse):
                    end += 1
                text = line[col:end].strip()
                if text:
                    elements.append(Element("text", label_of(text), row, col, end - col, screen=code))
                col = end
        return elements

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
            elif e.role == "button":
                line += f" (key {e.key})"
            lines.append(line)
        self._snapshot = "\n".join(lines)
        code = self._elements[0].screen if self._elements else ""
        return Observation(snapshot_id=f"s{self._snapshots}", url=f"{self.origin}/{code}", text=self._snapshot,
                           fingerprint=fingerprint(self._snapshot))

    async def screenshot(self, path: str) -> str | None:
        return None  # a terminal's evidence is its screen, kept as the (masked) snapshot

    def grid(self) -> str:
        """The screen as plain text, 24 lines: for a person, never stored unmasked."""
        return "\n".join(self.screen.display)

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
        candidates.append(BBoxStrategy(x=e.col, y=e.row, width=e.width, height=1))
        target = Target(frame=[e.screen], strategies=candidates)
        kept = [s for s in candidates if [(h.row, h.col) for h in self._hits(target, s, purpose)] == [(e.row, e.col)]]
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
                return [e for e in elements if e.role == role and _matches(name, e.name)]
            case LabelStrategy(label=label):
                return [e for e in elements if e.role == "textbox" and _matches(label, e.name)]
            case AnchorStrategy(anchor_text=anchor, relation=relation, role=role) if relation in ("right-of", "after"):
                hits = []
                for i, a in enumerate(elements):
                    if a.role == "text" and _matches(anchor, a.name):
                        nxt = next((e for e in elements[i + 1:] if (relation == "after" or e.row == a.row)
                                    and (role is None or e.role == role)), None)
                        if nxt and (relation == "after" or nxt.row == a.row):
                            hits.append(nxt)
                return hits
            case BBoxStrategy(x=x, y=y):
                return [e for e in elements if e.row == int(y) and e.col <= int(x) < e.col + max(e.width, 1)
                        and e.role != "heading"]
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
            if (e.row, e.col, e.role, e.screen) == (handle.row, handle.col, handle.role, handle.screen):
                return e
        raise SurfaceError("NOT_ACTIONABLE", f"{handle.role} {handle.name!r} is no longer on the screen", retry="safe")

    # ── Acting ─────────────────────────────────────────────────────────────────────────────────────────────────

    async def act(self, handle: object, action: Action, value: str | None = None,
                  guard: Callable[[], None] | None = None) -> None:
        e = self._live(handle)
        if action == "type" and e.role == "textbox":
            await self._type(e, value or "", guard)
        elif action in ("click", "press") and e.role == "button":
            if guard:
                guard()
            if reason := self._refused(e):
                self.blocked.append(reason)
                return
            mark = self._received
            await self._send(KEY_BYTES[e.key])
            await self._settle(mark)
        else:
            raise SurfaceError("NOT_ACTIONABLE", f"{action} isn't something a terminal {e.role} does")

    def _refused(self, key: Element) -> str | None:
        if self.policy is None or _SAFE_KEY.search(key.name):
            return None
        path = f"/{key.screen}"
        if any(fnmatch.fnmatch(path, p) for p in self.policy.commit_paths):
            return (f"blocked by policy: {key.key}={key.name} on {key.screen} would commit a change (irreversible). "
                    f"Automation can reach a commit point but never press it; stop here.")
        return None

    async def _type(self, field: Element, value: str, guard: Callable[[], None] | None):
        """Tab to the field, erase it, type, and check it holds exactly that (a hidden field shows one * a
        character). One more try, then a failure that names no value."""
        for _ in range(2):
            for _ in range(len(self._parse()) + 1):
                if self.screen.cursor.y == field.row and field.col <= self.screen.cursor.x < field.col + field.width:
                    break
                mark = self._received
                await self._send(TAB)
                await self._settle(mark, wait_s=1)
            else:
                raise SurfaceError("NOT_ACTIONABLE", f"can't reach the {field.name!r} field with Tab")
            if guard:
                guard()
            mark = self._received
            await self._send(ERASE + value.encode())
            await self._settle(mark)
            shown = self._live(field).value
            if shown == value or (shown and set(shown) == {"*"} and len(shown) == len(value)):
                return
        raise SurfaceError("NOT_ACTIONABLE", "the field didn't keep what was typed into it", retry="safe")

    async def read_text(self, handle: object) -> str:
        e = self._live(handle) if isinstance(handle, Element) and handle.role == "textbox" else handle
        if isinstance(e, Element) and e.role == "text":
            # Re-read the cells now: the screen may have filled in since it was resolved
            for live in self._parse():
                if (live.row, live.col, live.screen) == (e.row, e.col, e.screen) and live.role == "text":
                    return live.name
            return ""
        return e.text.strip()

    # ── Conditions ─────────────────────────────────────────────────────────────────────────────────────────────

    async def holds(self, condition: Visible) -> bool:
        for e in self._on_screen(condition.frame):
            if condition.role and e.role != condition.role:
                continue
            if condition.name is not None and not _matches(condition.name, e.name):
                continue
            if condition.label is not None and not (e.role == "textbox" and _matches(condition.label, e.name)):
                continue
            if condition.text is not None and not _matches(condition.text, e.text if e.role == "textbox" else e.name):
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
