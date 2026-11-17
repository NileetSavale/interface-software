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
import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import pyte

from interface.artifact.schema import Policy
from interface.surface.base import SurfaceError
from interface.surface.screen import _CODE, Element, ScreenSurface, label_of

ROWS, COLS = 24, 80
KEY_BYTES = {"Enter": b"\r", "F1": b"\x1bOP", "F2": b"\x1bOQ", "F3": b"\x1bOR", "F4": b"\x1bOS", "F5": b"\x1b[15~",
             "F6": b"\x1b[17~", "F7": b"\x1b[18~", "F8": b"\x1b[19~", "F9": b"\x1b[20~", "F10": b"\x1b[21~",
             "F11": b"\x1b[23~", "F12": b"\x1b[24~"}
TAB, ERASE = b"\t", b"\x15"
_KEY = re.compile(r"(Enter|F\d{1,2})=(\S+(?: \S+)*)")
QUIET_S = 0.12


class TerminalSurface(ScreenSurface):
    def __init__(self, policy: Policy | None = None):
        super().__init__(policy)
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task | None = None
        self._received = 0  # bytes received so far
        self._last = 0.0  # when the last bytes arrived

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
        """Until the host has stopped sending for a moment."""
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

    async def screenshot(self, path: str) -> str | None:
        return None  # a terminal's evidence is its screen, kept as the (masked) snapshot

    def grid(self) -> str:
        """The screen as plain text, 24 lines: for a person, never stored unmasked."""
        return "\n".join(self.screen.display)

    async def _press(self, button: Element):
        mark = self._received
        await self._send(KEY_BYTES[button.key])
        await self._settle(mark)

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
