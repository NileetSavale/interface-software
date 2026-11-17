"""The vision Surface: an app that exposes nothing but pixels, read by OCR and driven with a mouse and keyboard.

For Citrix-streamed desktops, Java Swing or Delphi clients and canvas-drawn pages there's no DOM and no
accessibility tree, only a picture. This builds the same flat list of elements the terminal surface builds
(screen.py), from the picture:

- OCR (RapidOCR, local, no network) finds the words and where they are;
- boxes come from the pixels: rows of a long unbroken run of not-background colour, stacked, are a rectangle. A
  near-white box is a field you can type in, named by the label to its left; a filled box with words in it is a
  button; a full-width band at the top holds the screen code (which scopes every target) and the title;
- every other word is text. Lines are words whose middles sit at the same height.

Positions are in the app's own logical pixels, so a recording made on the canvas in a browser describes the same
places on the native window, whatever the display's scaling.

Where this is weaker than a tree, it fails closed rather than guesses:
- a value is read again from a crop at two scales, and both reads must agree with high confidence and not look like
  a digit/letter confusion (0/O, 1/l, 5/S); otherwise the read is UNREADABLE and the run fails at that step. A
  misread balance returned as success would be worse than no answer;
- what's typed is read back off the screen before the step counts (a masked field: that it shows a mask);
- OCR can drop the spaces between words, so names match with whitespace ignored, never more loosely.

The model reads the OCR'd snapshot through the same masker as everything else, never the picture: sending
screenshots would send the record. Evidence screenshots black out every value first.
"""
import asyncio
import hashlib
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np

from interface.artifact.schema import BBoxStrategy, Policy, Regex, TextMatch, Visible
from interface.surface.base import Observation, SurfaceError
from interface.surface.screen import _CODE, Element, ScreenSurface, label_of

FRESH_S = 0.3  # a capture this recent is the screen as it is now
STEADY_S = 0.08  # two captures this far apart and identical: the screen has stopped changing
MIN_RUN = 40  # px: a run of not-background this long is a box edge or fill, not a glyph
SPACE_PX = 5  # a blank gap this wide between letters is a space (measured: letters ≤ 3, digits ≤ 4, spaces ≥ 5)
GAP = 16  # rows a box's edges may go missing for, through a line of text
MIN_CONFIDENCE = 0.85
_CONFUSABLE = re.compile(r"\d[OoIlSBZ]|[OoIlSBZ]\d")


class Unreadable(SurfaceError):
    """A value the surface couldn't read with confidence. Never guessed: the step fails."""

    def __init__(self, message: str):
        super().__init__("UNREADABLE", message)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True)
class Word:
    text: str
    confidence: float
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def middle(self) -> tuple[float, float]:
        return (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2


class Reader:
    """RapidOCR, loaded once per process (about a second)."""
    _engine = None

    @classmethod
    def engine(cls):
        if cls._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            cls._engine = RapidOCR()
        return cls._engine

    @classmethod
    def words(cls, image: np.ndarray) -> list[Word]:
        found, _ = cls.engine()(image)
        return [Word(text.strip(), float(conf), int(min(p[0] for p in box)), int(min(p[1] for p in box)),
                     int(max(p[0] for p in box)), int(max(p[1] for p in box)))
                for box, text, conf in found or [] if text.strip()]

    @classmethod
    def line(cls, image: np.ndarray) -> tuple[str, float]:
        """One line of text, recognition only (the crop is already the line)."""
        found, _ = cls.engine()(image, use_det=False, use_cls=False, use_rec=True)
        return (found[0][0].strip(), float(found[0][1])) if found else ("", 0.0)


def _recognise(image: np.ndarray, scale: int) -> tuple[str, float]:
    """One line of text from a crop: cut to its ink with a 2 px margin, then scaled. The recogniser reads Chinese
    as well as English, and given empty space beside a short value it will "see" a character there (1 → 1個), so
    the crop never has any."""
    from PIL import Image
    flat = image.reshape(-1, 3).astype(np.int32)
    packed = flat[:, 0] << 16 | flat[:, 1] << 8 | flat[:, 2]
    v, c = np.unique(packed, return_counts=True)
    bg = np.array([v[c.argmax()] >> 16 & 255, v[c.argmax()] >> 8 & 255, v[c.argmax()] & 255])
    ink = np.abs(image.astype(np.int32) - bg).max(axis=2) > 40
    if not ink.any():
        return "", 0.0
    ys, xs = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    m = 2
    cut = image[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]
    framed = np.empty((cut.shape[0] + 2 * m, cut.shape[1] + 2 * m, 3), dtype=np.uint8)
    framed[:] = bg
    framed[m:m + cut.shape[0], m:m + cut.shape[1]] = cut
    crop = Image.fromarray(framed)
    text, confidence = Reader.line(np.array(crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)))
    return text, confidence


def _merge_spaces(*readings: str) -> str:
    """Readings that spell the same characters, with a space wherever any of them has one."""
    base = _squash(readings[0])
    gaps = set()
    for reading in readings:
        i = 0
        for ch in reading:
            if ch.isspace():
                gaps.add(i)
            else:
                i += 1
    return "".join((" " if i in gaps and i else "") + ch for i, ch in enumerate(base))


def _letters_only(text: str) -> str:
    """Spaces in a value only between letters (or before an apostrophe): one OCR imagines in a number would change
    it."""
    return "".join(ch for i, ch in enumerate(text) if ch != " " or (
        0 < i < len(text) - 1 and text[i - 1].isalpha() and (text[i + 1].isalpha() or text[i + 1] == "'")))


def boxes(image: np.ndarray) -> list[tuple[int, int, int, int, tuple[int, int, int]]]:
    """Rectangles drawn on the background: (x0, y0, x1, y1, fill colour)."""
    h, w, _ = image.shape
    sample = image[::4, ::4].reshape(-1, 3).astype(np.int32)
    packed = sample[:, 0] << 16 | sample[:, 1] << 8 | sample[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    bg = values[counts.argmax()]
    background = np.array([bg >> 16 & 255, bg >> 8 & 255, bg & 255], dtype=np.int32)
    other = np.abs(image.astype(np.int32) - background).max(axis=2) > 8
    # (x0, x1) -> (top, last row seen). A box's rows through its text can break where anti-aliased glyphs pass
    # through the background's colour, so a box survives a gap of up to GAP rows
    open_: dict[tuple[int, int], tuple[int, int]] = {}
    done = []
    for y in range(h):
        row = np.concatenate(([False], other[y], [False]))
        edges = np.flatnonzero(row[1:] != row[:-1])
        for a, b in ((int(a), int(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= MIN_RUN):
            key = next((k for k in open_ if abs(k[0] - a) <= 2 and abs(k[1] - b) <= 2), None)
            top = open_.pop(key)[0] if key else y
            open_[(a, b)] = (top, y)
        for k, (top, last) in list(open_.items()):
            if y - last > GAP:
                done.append((k[0], top, k[1], last + 1))
                del open_[k]
    done += [(a, top, b, last + 1) for (a, b), (top, last) in open_.items()]
    found = []
    for x0, y0, x1, y1 in done:
        if y1 - y0 < 12:
            continue
        inner = image[y0 + 2:y1 - 2, x0 + 2:x1 - 2].reshape(-1, 3)
        if not len(inner):
            continue
        packed = inner[:, 0].astype(np.int32) << 16 | inner[:, 1].astype(np.int32) << 8 | inner[:, 2]
        v, c = np.unique(packed, return_counts=True)
        fill = int(v[c.argmax()])
        found.append((int(x0), int(y0), int(x1), int(y1), (fill >> 16 & 255, fill >> 8 & 255, fill & 255)))
    return found


# ── Where the pixels come from ─────────────────────────────────────────────────────────────────────────────────


class CanvasScreen:
    """A page in Chromium, read from screenshots only: its <canvas> if it draws itself on one, else the whole
    viewport (an ordinary page treated as a picture, as a Citrix window would be). Mouse and keyboard out. Nothing is
    read from the DOM: the only thing asked of it is where the canvas is, to aim clicks."""
    scale = 1.0

    def __init__(self, policy: Policy | None = None, blocked: list[str] | None = None):
        self._pw = self.browser = self.page = None
        self.offset = (0.0, 0.0)
        self.policy, self.blocked = policy, blocked if blocked is not None else []

    async def open(self, url: str):
        if self.page is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch()
            context = await self.browser.new_context(viewport={"width": 1280, "height": 900})
            if self.policy:
                # The web surface's network gate, unchanged: it reads no page, it refuses requests. A click on
                # pixels that would post to a commit path, or load another origin, never leaves the browser
                from interface.policy import refused

                async def gate(route):
                    request = route.request
                    document = request.is_navigation_request() or request.method != "GET"
                    if reason := refused(self.policy, request.url, document):
                        self.blocked.append(reason)
                        await route.abort("blockedbyclient")
                    else:
                        await route.continue_()
                await context.route("**/*", gate)
            self.page = await context.new_page()
        await self.page.goto(url)
        self.offset = (0.0, 0.0)
        if await self.page.locator("canvas").count():
            box = await self.page.locator("canvas").first.bounding_box()
            self.offset = (box["x"], box["y"]) if box else (0.0, 0.0)

    async def capture(self) -> np.ndarray:
        from io import BytesIO

        from PIL import Image
        box = await self.page.locator("canvas").first.bounding_box() \
            if await self.page.locator("canvas").count() else None
        png = await self.page.screenshot(clip=box) if box else await self.page.screenshot()
        return np.array(Image.open(BytesIO(png)).convert("RGB"))

    async def click(self, x: float, y: float):
        await self.page.mouse.click(self.offset[0] + x, self.offset[1] + y)

    async def key(self, name: str):
        await self.page.keyboard.press(name)

    async def type(self, text: str):
        await self.page.keyboard.type(text)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()
        self._pw = self.browser = self.page = None


class DesktopScreen:
    """A native window (apps/mock_gui/desktop.py, Tk), driven with real OS input: the screen captured with mss, the
    mouse and keyboard with pyautogui. desktop://host:port starts the client against that server, fresh, as a
    terminal reconnects. On macOS the terminal running this needs Screen Recording and Accessibility permission."""

    def __init__(self):
        import pyautogui
        pyautogui.PAUSE = 0.02  # its default waits 0.1 s after every call
        self.proc: subprocess.Popen | None = None
        self.region = (0, 0, 0, 0)  # the drawing area on screen, in points
        self.scale = 1.0  # captured pixels per point (2 on a Retina display)

    async def open(self, url: str):
        await self.close()
        parts = urlsplit(url)
        self.proc = subprocess.Popen([sys.executable, "-m", "apps.mock_gui.desktop", "--server",
                                      f"http://{parts.netloc}"], stdout=subprocess.PIPE, text=True)
        line = await asyncio.wait_for(asyncio.to_thread(self.proc.stdout.readline), timeout=20)
        if not line.startswith("READY"):
            raise SurfaceError("TIMEOUT", "the desktop client didn't open its window", retry="safe")
        self.region = tuple(int(v) for v in line.split()[1:5])
        await asyncio.sleep(0.5)
        image = await self.capture()
        self.scale = image.shape[1] / self.region[2]
        await self.click(self.region[2] / 2, 30)  # bring the window to the front, on its title band

    async def capture(self) -> np.ndarray:
        import mss

        def grab():
            x, y, w, h = self.region
            with mss.mss() as shot:
                return np.array(shot.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, 2::-1].copy()
        return await asyncio.to_thread(grab)

    async def click(self, x: float, y: float):
        import pyautogui
        await asyncio.to_thread(pyautogui.click, self.region[0] + x, self.region[1] + y)

    async def key(self, name: str):
        import pyautogui
        keys = [k.lower().replace("control", "ctrl") for k in name.split("+")]
        await asyncio.to_thread(pyautogui.hotkey, *keys) if len(keys) > 1 else \
            await asyncio.to_thread(pyautogui.press, keys[0])

    async def type(self, text: str):
        import pyautogui
        await asyncio.to_thread(pyautogui.write, text, 0.02)

    async def close(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(5)
            self.proc = None


# ── The surface ────────────────────────────────────────────────────────────────────────────────────────────────


class VisionSurface(ScreenSurface):
    def __init__(self, policy: Policy | None = None):
        super().__init__(policy)
        self.pixels: CanvasScreen | DesktopScreen | None = None
        self.image: np.ndarray | None = None
        self._hash = ""
        self._captured = 0.0  # when the screen was last captured
        self._parsed: dict[str, list[Element]] = {}  # by image hash: OCR runs once per distinct screen
        self._words: dict[str, list[Word]] = {}

    # ── The connection ─────────────────────────────────────────────────────────────────────────────────────────

    async def navigate(self, url: str) -> None:
        """Open the app afresh at its first screen: the page in a browser, or the native client."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https", "desktop"):
            raise SurfaceError("NAVIGATION_BLOCKED", f"a pixel-only app is http(s):// or desktop://, not {url}")
        kind = DesktopScreen if parts.scheme == "desktop" else CanvasScreen
        if not isinstance(self.pixels, kind):
            await self.close()
            self.pixels = DesktopScreen() if kind is DesktopScreen else CanvasScreen(self.policy, self.blocked)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        try:
            await self.pixels.open(url)
        except SurfaceError:
            raise
        except Exception as e:
            raise SurfaceError("TIMEOUT", f"can't open {self.origin}: {type(e).__name__}", retry="safe") from e
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await self._quiet()
            if self.code:
                return
            await asyncio.sleep(0.1)
        hint = (" (on macOS a capture without Screen Recording permission shows only the wallpaper: allow it for "
                "this terminal in System Settings → Privacy & Security)") if isinstance(self.pixels, DesktopScreen) else ""
        raise SurfaceError("TIMEOUT", f"{self.origin} showed no screen{hint}", retry="safe")

    async def close(self):
        if self.pixels:
            await self.pixels.close()

    async def _capture(self) -> str:
        self.image = await self.pixels.capture()
        self._hash = hashlib.sha1(self.image.tobytes()).hexdigest()
        self._captured = time.monotonic()
        return self._hash

    async def _quiet(self, wait_s: float = 2.0):
        """Until two captures in a row are identical."""
        deadline = time.monotonic() + wait_s
        last = await self._capture()
        while time.monotonic() < deadline:
            await asyncio.sleep(STEADY_S)
            if (now := await self._capture()) == last:
                break
            last = now
        if self._hash not in self._parsed:
            self._parsed[self._hash] = await asyncio.to_thread(self._read, self.image)

    async def _settle(self, before: str, wait_s: float = 3.0):
        """After an action: until the screen has changed from before and stopped changing, or wait_s passes."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and await self._capture() == before:
            await asyncio.sleep(0.05)
        await self._quiet()

    # ── Reading the picture ────────────────────────────────────────────────────────────────────────────────────

    def _read(self, image: np.ndarray) -> list[Element]:
        # Detection over the whole screen finds the words; each is then read again from its own crop, which keeps
        # the spaces whole-screen recognition can drop ("JaneDoe")
        # the spaces whole-screen recognition can drop ("JaneDoe"), placed the way read_text places them, so the
        # snapshot shows a value exactly as an extraction will read it
        s = self.pixels.scale if self.pixels else 1.0
        words = []
        for w in Reader.words(image):
            crop = image[max(0, w.y0 - 2):w.y1 + 2, max(0, w.x0 - 3):w.x1 + 3]
            readings = [w.text]
            again = _recognise(crop, 1)[0]
            if _squash(again) == _squash(w.text):
                readings.append(again)
            try:
                readings.append(self._spaced(crop, _squash(w.text), s))
            except Unreadable:
                pass
            text = _merge_spaces(*readings)
            words.append(Word(text, w.confidence, w.x0, w.y0, w.x1, w.y1))
        self._words[hashlib.sha1(image.tobytes()).hexdigest()] = words
        width = image.shape[1]
        rects = boxes(image)
        band = next((r for r in rects if r[2] - r[0] >= 0.6 * width and r[1] <= 4), None)

        def inside(word: Word, r) -> bool:
            mx, my = word.middle
            return r[0] <= mx < r[2] and r[1] <= my < r[3]

        placed: list[tuple[str, float, float, float, float, str, bool]] = []  # role, x, y, w, h, text, hidden
        used: set[int] = set()
        code = ""
        if band:
            mine = sorted((w for w in words if inside(w, band)), key=lambda w: w.x0)
            used |= {id(w) for w in mine}
            code = mine[0].text if mine and _CODE.match(mine[0].text) else ""
            for i, w in enumerate(mine):
                heading = i == (1 if code else 0)
                placed.append(("heading" if heading else "text", w.x0, w.y0, w.x1 - w.x0, w.y1 - w.y0, w.text, False))
        for r in rects:
            if r is band or r[2] - r[0] >= 0.6 * width:
                continue
            mine = sorted((w for w in words if inside(w, r) and id(w) not in used), key=lambda w: w.x0)
            used |= {id(w) for w in mine}
            text = " ".join(w.text for w in mine)
            if min(r[4]) >= 245:  # a white well: a field, whatever's in it
                # Read from its own crop: detection over the whole screen misses one short character ("1"). Ink
                # but no letters or digits: a masked field (OCR reads a row of * as nothing, or as punctuation)
                inner = image[r[1] + 3:r[3] - 3, r[0] + 3:r[2] - 3]
                ink = inner.max(axis=2) < 128
                text, hidden = "", False
                if ink.sum() > 4:
                    text = re.sub(r"[^\x20-\x7e]", "", _recognise(inner, 1)[0]).strip()
                    hidden = not re.search(r"[0-9A-Za-z]", text)
                    text = "*" * max(1, round(ink.any(axis=0).sum() / 8)) if hidden else text
                placed.append(("textbox", r[0], r[1], r[2] - r[0], r[3] - r[1], text, hidden))
            elif text:
                placed.append(("button", r[0], r[1], r[2] - r[0], r[3] - r[1], text, False))
        for w in words:
            if id(w) not in used:
                placed.append(("text", w.x0, w.y0, w.x1 - w.x0, w.y1 - w.y0, w.text, False))
        # Lines: elements whose middles sit at the same height
        placed.sort(key=lambda p: p[2] + p[4] / 2)
        rows, row, top = [], -1, None
        for p in placed:
            middle = p[2] + p[4] / 2
            if top is None or middle - top > 12 * s:
                row, top = row + 1, middle
            rows.append((row, p))
        elements: list[Element] = []
        for row, (role, x, y, w, h, text, hidden) in sorted(rows, key=lambda rp: (rp[0], rp[1][1])):
            box = (round(x / s), round(y / s), round(w / s), round(h / s))
            if role == "textbox":
                before = next((e for e in reversed(elements) if e.row == row and e.role == "text"), None)
                name = label_of(before.name).rstrip(":").strip() if before and before.name.endswith(":") else ""
                elements.append(Element("textbox", name, row, box[0], box[2], text, screen=code, box=box,
                                        hidden=hidden))
            else:
                elements.append(Element(role, label_of(text) if role == "text" else text, row, box[0], box[2],
                                        screen=code, box=box))
        return elements

    def _parse(self) -> list[Element]:
        return self._parsed.get(self._hash, [])

    async def observe(self, *, screenshot: bool = False, screenshot_path: str | None = None) -> Observation:
        observation = await super().observe()
        if screenshot_path:
            observation = observation.model_copy(update={"screenshot_path": await self.screenshot(screenshot_path)})
        return observation

    async def screenshot(self, path: str) -> str | None:
        """The screen with every value blacked out: field contents, and any text that isn't a label, a title or a
        button. Labels and layout stay, so the evidence shows where the run was without whose record it was."""
        try:
            from PIL import Image
            await self._quiet()
            image = self.image.copy()
            s = self.pixels.scale
            for e in self._parse():
                if e.box and (e.role == "textbox" or (e.role == "text" and not e.name.endswith(":")
                                                      and e.name != e.screen)):
                    x, y, w, h = (round(v * s) for v in e.box)
                    pad = round(3 * s)
                    image[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad] = 0
            Image.fromarray(image).save(path)
            return path
        except Exception:
            return None  # fail closed: no screenshot rather than an unmasked one

    # ── Matching, positions ────────────────────────────────────────────────────────────────────────────────────

    def _match(self, match: TextMatch, text: str) -> bool:
        if isinstance(match, Regex):
            return re.search(match.matches, text) is not None
        return _squash(match) == _squash(text)

    def _bbox(self, e: Element) -> BBoxStrategy:
        x, y, w, h = e.box
        return BBoxStrategy(x=x, y=y, width=w, height=h)

    def _at(self, elements: list[Element], x: float, y: float, w: float, h: float) -> list[Element]:
        """Elements on the recorded box's line that start where it started, or that hold its centre: a value that's
        longer or shorter today, left-aligned as values are, still hits; a neighbour it merely overlaps doesn't."""
        cx, cy = x + w / 2, y + h / 2
        return [e for e in elements if e.box and e.role != "heading" and e.box[1] - 4 <= cy <= e.box[1] + e.box[3] + 4
                and (abs(e.box[0] - x) <= 8 or e.box[0] <= cx < e.box[0] + e.box[2])]

    def _same(self, a: Element, b: Element) -> bool:
        if a.screen != b.screen or not (a.box and b.box):
            return False
        return abs(a.box[0] - b.box[0]) <= 10 and abs(a.box[1] - b.box[1]) <= 10

    # ── Acting ─────────────────────────────────────────────────────────────────────────────────────────────────

    def _centre(self, e: Element) -> tuple[float, float]:
        x, y, w, h = e.box
        return x + w / 2, y + h / 2

    async def _press(self, button: Element):
        before = self._hash or await self._capture()
        await self.pixels.click(*self._centre(button))
        await self._settle(before)

    async def _type(self, field: Element, value: str, guard: Callable[[], None] | None):
        """Click into the field, select what's there, type over it, and read it back off the screen."""
        for _ in range(2):
            await self.pixels.click(*self._centre(field))
            if guard:
                guard()
            await self.pixels.key("Control+A")
            before = await self._capture()
            await self.pixels.type(value)
            await self._settle(before, wait_s=1.5)
            shown = self._live(field).value
            if _squash(shown) == _squash(value) or (value and shown and not re.search(r"[0-9A-Za-z]", shown)):
                return  # the value, or (a masked field) a mask
        raise SurfaceError("NOT_ACTIONABLE", "the field didn't keep what was typed into it", retry="safe")

    async def read_text(self, handle: object) -> str:
        """A value, read again from a crop at two scales; both reads must agree, with confidence."""
        await self._quiet()
        e = self._live(handle) if isinstance(handle, Element) else handle
        if not isinstance(e, Element) or e.role == "textbox":
            return e.text.strip() if isinstance(e, Element) else str(e)
        live = next((x for x in self._parse() if x.role == "text" and self._same(x, e)), None)
        if live is None or not live.name:
            return ""
        s = self.pixels.scale
        x, y, w, h = (round(v * s) for v in live.box)
        pad = round(4 * s)
        crop = self.image[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
        reads = [await asyncio.to_thread(_recognise, crop, k) for k in (1, 2)]
        texts = {_squash(t) for t, _ in reads}
        if len(texts) != 1 or min(c for _, c in reads) < MIN_CONFIDENCE:
            raise Unreadable("the value was read two ways and they disagreed, or one wasn't confident")
        if re.search(r"[^\x20-\x7e]", reads[0][0]):
            raise Unreadable("the value read as characters this app doesn't show")
        spaced = await asyncio.to_thread(self._spaced, crop, _squash(reads[0][0]), s)
        text = _letters_only(_merge_spaces(spaced, *(t for t, _ in reads)))
        if len(re.findall(r"\d", text)) >= 3 and _CONFUSABLE.search(text):
            raise Unreadable("the value mixes digits with letters OCR confuses with them (0/O, 1/l, 5/S)")
        return text

    @staticmethod
    def _spaced(crop: np.ndarray, agreed: str, scale: float) -> str:
        """Where the spaces go, from the pixels: OCR drops them ("Margarethe Doe" → "MargaretheDoe"), and in a name
        that's a wrong value. The crop is cut at blank gaps of SPACE_PX or more, each piece read alone, and the pieces
        must spell what the whole reads agreed on. A space goes back only between two letters, so a wide gap in a
        number ("4, 210") never changes it."""
        flat = crop.reshape(-1, 3).astype(np.int32)
        packed = flat[:, 0] << 16 | flat[:, 1] << 8 | flat[:, 2]
        v, c = np.unique(packed, return_counts=True)
        bg = np.array([v[c.argmax()] >> 16 & 255, v[c.argmax()] >> 8 & 255, v[c.argmax()] & 255])
        ink = (np.abs(crop.astype(np.int32) - bg).max(axis=2) > 40).any(axis=0)
        columns = np.flatnonzero(ink)
        if not len(columns):
            return agreed
        pieces, start, last = [], columns[0], columns[0]
        for col in columns[1:]:
            if col - last - 1 >= SPACE_PX * scale:
                pieces.append((start, last + 1))
                start = col
            last = col
        pieces.append((start, last + 1))
        if len(pieces) == 1:
            return agreed
        pad = round(2 * scale)
        words = [_recognise(crop[:, max(0, a - pad):b + pad], 1)[0] for a, b in pieces]
        if _squash("".join(words)) != agreed:
            raise Unreadable("the value's words didn't read the same one by one as all together")
        text = words[0]
        for word in words[1:]:
            text += (" " if text[-1:].isalpha() and (word[:1].isalpha() or word[:1] == "'") else "") + word
        return text

    # ── Conditions: on a fresh capture ─────────────────────────────────────────────────────────────────────────

    async def _fresh(self):
        """Capture again unless the screen was just captured: replay checks several conditions in a row, and each
        waiting for a steady screen of its own made every step take seconds."""
        if time.monotonic() - self._captured > FRESH_S:
            await self._quiet(wait_s=0.5)

    async def holds(self, condition: Visible) -> bool:
        await self._fresh()
        return await super().holds(condition)

    async def gone(self, handle: object, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            await self._quiet(wait_s=0.5)
            try:
                self._live(handle)
            except SurfaceError:
                return
            await asyncio.sleep(0.1)

    async def headings(self) -> list[Visible]:
        await self._fresh()
        return await super().headings()
