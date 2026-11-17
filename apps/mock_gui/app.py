"""A stand-in for a core-banking desktop client that exposes nothing but pixels: no DOM text, no accessibility tree,
no ids. Fake data only. Run with:  uv run interface mock-gui --port 4400

The same host as the green screen (apps/mock_terminal: its screens, members, rules and faults), with a GUI front end
drawn on top, as many legacy systems have one: labels, white input wells, buttons, a title band with the screen code.
The server lays each screen out once, as a spec of things to draw at pixel positions (spec()), and two clients draw
it the same way:

- the browser page at /, onto a <canvas>: what the evals and CI drive, through Playwright's mouse, keyboard and
  screenshots only;
- a native Tk window (apps/mock_gui/desktop.py): what the desktop demo drives with real OS input.

Either way a client keeps what's typed until a button is pressed, like a form, then posts the fields and the key.
Keys: click a field or Tab to it, Ctrl/Cmd+A selects its contents (the next character replaces them), Backspace,
Enter presses the screen's Enter button, F1-F12 their buttons.

Controls for tests (not part of the app): POST /__faults?name=late_fill&times=1, DELETE /__faults,
POST /__variant?name=b. Variant B is a second bank on the same product: the terminal's relabels ("Mbr Number", Find)
plus its own look, a different font, colours and button style, with every control where it was.
GET /__values?seed=7 lays out a screen of random names, dates and amounts with the truth beside it, for measuring
how values are read (evals/vision.py).
"""
import asyncio
import random
import re
import secrets

from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from apps.mock_terminal.app import FAULTS, Screen, Session, TerminalBank
from interface.serverlog import ServerLog

X0, CW, Y0, RH = 24, 10, 52, 27  # left margin, px per terminal column, top of row 1, px per row
WIDTH, HEIGHT = 960, 720
KEY = re.compile(r"(Enter|F\d{1,2})=(\S+(?: \S+)*)")
SPLIT = re.compile(r"^(?P<label>.*?\S)(?:\s*\.)*\s+:\s+(?P<value>\S.*)$")  # "Name . . . :  Jane Doe"
LEADER = re.compile(r"(?:\s*\.)*\s*:$")
THEMES = {
    "a": {"bg": "#d4d0c8", "band": "#1f3c78", "bandText": "#ffffff", "text": "#000000", "field": "#ffffff",
          "fieldBorder": "#6b6b6b", "button": "#e4e4e4", "buttonBorder": "#6b6b6b", "buttonText": "#000000",
          "message": "#8b0000", "font": "Arial", "size": 16},
    "b": {"bg": "#edf2f7", "band": "#0b6e4f", "bandText": "#ffffff", "text": "#1a202c", "field": "#ffffff",
          "fieldBorder": "#94a3b8", "button": "#2b6cb0", "buttonBorder": "#2b6cb0", "buttonText": "#ffffff",
          "message": "#9b2c2c", "font": "Verdana", "size": 15},
}


def _label(text: str) -> str:
    return LEADER.sub(":", text.strip()) if LEADER.search(text) else text.strip()


def spec(screen: Screen, variant: str) -> dict:
    """Where everything on this screen is drawn, in pixels."""
    bank = "Second Bank" if variant == "b" else "First Bank"
    texts = [{"text": screen.code, "x": 16, "y": 12, "band": True},
             {"text": screen.title.title(), "x": 180, "y": 12, "band": True},
             {"text": bank, "x": WIDTH - 140, "y": 12, "band": True}]
    for row, line in sorted(screen.lines.items()):
        y = Y0 + row * RH
        if m := SPLIT.match(line):
            texts.append({"text": _label(m["label"] + " :"), "x": X0, "y": y})
            texts.append({"text": m["value"], "x": X0 + 22 * CW, "y": y})
        elif line.strip():
            texts.append({"text": _label(line), "x": X0 + (len(line) - len(line.lstrip())) * CW, "y": y})
    fields = [{"name": f.name, "x": X0 + f.col * CW, "y": Y0 + f.row * RH - 5, "w": max(f.width * CW + 16, 64),
               "h": 26, "value": f.value, "hidden": f.hidden, "max": f.width} for f in screen.fields]
    buttons, x = [], X0
    for m in KEY.finditer(screen.keys):
        w = len(m[2]) * 10 + 36
        buttons.append({"key": m[1], "label": m[2], "x": x, "y": HEIGHT - 60, "w": w, "h": 34})
        x += w + 16
    if screen.message:
        texts.append({"text": screen.message, "x": X0, "y": HEIGHT - 104, "message": True})
    return {"code": screen.code, "width": WIDTH, "height": HEIGHT, "theme": THEMES.get(variant, THEMES["a"]),
            "texts": texts, "fields": fields, "buttons": buttons}


class _Writer:
    """The terminal session writes to a socket; here nothing is written, the screen is kept and drawn by a client."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


class GuiSession(Session):
    def __init__(self, bank: "GuiBank"):
        super().__init__(bank, None, _Writer())
        self.version = 0

    async def show(self, screen: Screen):
        self.screen, self.focus = screen, 0
        self.version += 1
        self.bank.log.event("screen", session=self.id, screen=screen.code, variant=self.bank.variant_name,
                            message=screen.message or None)

    async def key(self, name: str):
        if name == "F3" and self.screen.code in ("CB0100", "CB0200"):  # the terminal hangs up; a window says so
            self.bank.log.event("key", session=self.id, screen=self.screen.code, key=name)
            await self.show(Screen("CB0000", "SIGNED OFF", {6: "The session has ended. Reopen to sign on again."}))
            return
        await super().key(name)


class GuiBank(TerminalBank):
    def __init__(self, variant: str = "a", always: str | None = None):
        super().__init__(variant, always)
        self.log = ServerLog("mock-gui")
        self.live: dict[str, GuiSession] = {}

    async def open(self) -> str:
        session = GuiSession(self)
        sid = secrets.token_hex(8)
        self.live[sid] = session
        self.log.event("connected", session=session.id)
        await session.show(session.sign_on())
        return sid


BANK = GuiBank()
app = FastAPI()


@app.post("/api/session")
async def new_session():
    return {"sid": await BANK.open()}


@app.get("/api/screen")
async def screen(sid: str):
    session = BANK.live.get(sid)
    if session is None:
        return JSONResponse({"error": "no such session"}, status_code=404)
    return {"version": session.version, "spec": spec(session.screen, BANK.variant_name)}


@app.post("/api/key")
async def key(sid: str = Body(), key: str = Body(), fields: dict[str, str] = Body(default_factory=dict)):
    session = BANK.live.get(sid)
    if session is None:
        return JSONResponse({"error": "no such session"}, status_code=404)
    for f in session.screen.fields:
        f.value = str(fields.get(f.name, f.value))[:f.width]
    asyncio.create_task(session.key(key))  # a slow or late-filling screen arrives on a later poll
    return {"ok": True}


@app.post("/__faults")
def arm_fault(name: str, times: int = 1):
    if name not in FAULTS:
        return JSONResponse({"error": f"unknown fault; one of {sorted(FAULTS)}"}, status_code=400)
    BANK.fault(name, times)
    BANK.log.event("fault_armed", fault=name, times=times)
    return {"armed": name, "times": times}


@app.delete("/__faults")
def clear_faults():
    BANK.fault(None)
    return {"armed": None}


@app.post("/__variant")
def set_variant(name: str):
    if name not in THEMES:
        return JSONResponse({"error": f"unknown variant; one of {sorted(THEMES)}"}, status_code=400)
    BANK.variant(name)
    BANK.log.event("variant_set", variant=name)
    return {"variant": name}


@app.get("/__confirmed")
def confirmed():
    return {"count": BANK.confirmed}


FIRST = ["Jane", "Rafael", "Mei", "Ana", "Oluwaseun", "Li", "Margarethe", "Tomasz", "Priya", "Ngozi", "Siobhan"]
LAST = ["Doe", "Ortiz", "Chen", "Kowalczyk", "Adeyemi", "Nakamura", "O'Brien", "Lindqvist", "Okafor", "Ruiz"]


def values_screen(seed: int, variant: str) -> tuple[dict, dict]:
    """A detail screen of random values, and what it says: for measuring reads, never for a flow."""
    rng = random.Random(seed)
    truth = {"memberNumber": f"{rng.randint(10000, 99999)}",
             "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
             "dob": f"{rng.randint(1, 12):02d}/{rng.randint(1, 28):02d}/{rng.randint(1930, 2005)}",
             "savings": f"{rng.randint(0, 999999) / 100:,.2f}",
             "checking": f"{rng.randint(0, 99999999) / 100:,.2f}"}
    rows = {6: f"Member # . . . . :  {truth['memberNumber']}", 8: f"Name . . . . . . :  {truth['name']}",
            9: f"Date of birth . . :  {truth['dob']}", 11: f"Savings balance . :  {truth['savings']}",
            12: f"Checking balance :  {truth['checking']}"}
    return spec(Screen("CB0310", "MEMBER DETAIL", rows, [], "F3=Exit  F12=Cancel"), variant), truth


@app.get("/__values")
def values(seed: int, variant: str = "a"):
    shown, truth = values_screen(seed, variant)
    return {"spec": shown, "truth": truth}


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Core Banking</title>
<style>html,body{margin:0;background:#888}canvas{display:block;outline:none}</style></head>
<body><canvas id="screen" width="960" height="720" tabindex="0"></canvas>
<script>
// Everything on screen is drawn here; nothing of it is in the DOM. Typing is kept locally until a button is pressed.
const canvas = document.getElementById("screen"), ctx = canvas.getContext("2d");
const params = new URLSearchParams(location.search);
let sid = null, version = -1, spec = null, values = {}, focus = 0, selectAll = false;

function draw() {
  const t = spec.theme, font = `${t.size}px ${t.font}`;
  ctx.fillStyle = t.bg; ctx.fillRect(0, 0, spec.width, spec.height);
  ctx.fillStyle = t.band; ctx.fillRect(0, 0, spec.width, 42);
  ctx.font = font; ctx.textBaseline = "top";
  for (const x of spec.texts) {
    ctx.fillStyle = x.band ? t.bandText : x.message ? t.message : t.text;
    ctx.fillText(x.text, x.x, x.y);
  }
  spec.fields.forEach((f, i) => {
    ctx.fillStyle = t.field; ctx.fillRect(f.x, f.y, f.w, f.h);
    ctx.strokeStyle = i === focus ? "#000000" : t.fieldBorder; ctx.lineWidth = 1;
    ctx.strokeRect(f.x + 0.5, f.y + 0.5, f.w - 1, f.h - 1);
    const v = values[f.name] || "";
    ctx.fillStyle = t.text; ctx.fillText(f.hidden ? "*".repeat(v.length) : v, f.x + 6, f.y + 5);
  });
  for (const b of spec.buttons) {
    ctx.fillStyle = t.button; ctx.fillRect(b.x, b.y, b.w, b.h);
    ctx.strokeStyle = t.buttonBorder; ctx.strokeRect(b.x + 0.5, b.y + 0.5, b.w - 1, b.h - 1);
    ctx.fillStyle = t.buttonText; ctx.fillText(b.label, b.x + 18, b.y + 9);
  }
}

function render(next) {
  spec = next; values = Object.fromEntries(spec.fields.map(f => [f.name, f.value])); focus = 0; selectAll = false;
  draw();
}

async function poll() {
  try {
    const r = await fetch(`/api/screen?sid=${sid}`);
    if (r.ok) { const j = await r.json(); if (j.version !== version) { version = j.version; render(j.spec); } }
  } catch (e) {}
  setTimeout(poll, 100);
}

function press(key) {
  fetch("/api/key", {method: "POST", headers: {"Content-Type": "application/json"},
                     body: JSON.stringify({sid, key, fields: values})});
}

canvas.addEventListener("mousedown", e => {
  const x = e.offsetX, y = e.offsetY, inside = r => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
  const f = spec.fields.findIndex(inside);
  if (f >= 0) { focus = f; selectAll = false; draw(); return; }
  const b = spec.buttons.find(inside);
  if (b) press(b.key);
});

canvas.addEventListener("keydown", e => {
  const f = spec.fields[focus];
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { selectAll = true; e.preventDefault(); return; }
  if (e.key === "Tab" && spec.fields.length) {
    focus = (focus + (e.shiftKey ? spec.fields.length - 1 : 1)) % spec.fields.length; selectAll = false;
  } else if (e.key === "Enter" || /^F\\d{1,2}$/.test(e.key)) {
    const b = spec.buttons.find(b => b.key === e.key);
    if (b) press(b.key);
  } else if (e.key === "Backspace" && f) {
    values[f.name] = selectAll ? "" : (values[f.name] || "").slice(0, -1); selectAll = false;
  } else if (e.key.length === 1 && f && !e.ctrlKey && !e.metaKey) {
    const v = selectAll ? "" : (values[f.name] || "");
    if (v.length < f.max) values[f.name] = v + e.key;
    selectAll = false;
  } else return;
  e.preventDefault(); draw();
});

(async () => {
  canvas.focus();
  if (params.has("values")) {  // the read-measuring screen: drawn once, no session
    const r = await fetch(`/__values?seed=${params.get("values")}&variant=${params.get("variant") || "a"}`);
    render((await r.json()).spec); version = 0; return;
  }
  sid = (await (await fetch("/api/session", {method: "POST"})).json()).sid;
  poll();
})();
</script></body></html>
"""
