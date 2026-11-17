"""The native demo: the artifact Claude recorded on the canvas client, replayed with no model on a native Tk window,
driven with real OS mouse and keyboard input, and recorded to evidence/desktop-demo/demo.mp4.

    uv sync --extra desktop
    uv run python scripts/desktop_demo.py

macOS only, and the terminal running it needs Screen Recording and Accessibility permission (System Settings →
Privacy & Security). Only the demo window's rectangle is recorded, never the rest of the screen; the window stays on
top while it runs. Don't touch the mouse or keyboard until it's done. The video shows the mock's fake members
unmasked: it's a demo recording. The runs' own evidence screenshots are masked as always.
"""
import asyncio
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from apps.mock_gui.app import BANK  # noqa: E402
from evals.common import ENV, label  # noqa: E402
from interface.artifact.io import load_artifact  # noqa: E402
from interface.evidence import Audit  # noqa: E402
from interface.replay.executor import Replayer  # noqa: E402
from interface.surface.vision import VisionSurface  # noqa: E402
from tests.conftest import free_port  # noqa: E402

OUT = ROOT / "evidence" / "desktop-demo"
ARTIFACT = ROOT / "artifacts" / "core_banking_gui.lookup_member_balance.json"
RUNS = [  # (what it shows, member, bank, fault)
    ("a member's balance, bank A", "34567", "a", None),
    ("a member number that doesn't exist", "99999", "a", None),
    ("the same lookup at bank B: new labels, font and colours", "34567", "b", None),
    ("the host shows someone else's record", "23456", "a", "wrong_record"),
]


def screen_device() -> str:
    """The avfoundation index of the main display, found by name: never a camera."""
    listing = subprocess.run(["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                             capture_output=True, text=True).stderr
    match = re.search(r"\[(\d+)\] Capture screen 0", listing)
    if not match:
        raise SystemExit("no 'Capture screen 0' device: is Screen Recording permission granted?")
    return match.group(1)


def capture_scale(device: str) -> float:
    """Pixels the screen device captures per point (2 on a Retina display), measured, never assumed: one frame
    decoded to nowhere, nothing saved."""
    import pyautogui
    probe = subprocess.run(["ffmpeg", "-f", "avfoundation", "-framerate", "15", "-i", f"{device}:none",
                            "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True).stderr
    width = int(re.search(r"Video: .*?, (\d+)x\d+", probe).group(1))
    return width / pyautogui.size().width


async def window(base: str) -> tuple[tuple[int, int, int, int], float]:
    surface = VisionSurface()
    await surface.navigate(base)
    region, scale = surface.pixels.region, surface.pixels.scale
    await surface.close()
    return region, scale


def main() -> int:
    server = uvicorn.Server(uvicorn.Config("apps.mock_gui.app:app", port=free_port(), log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"desktop://127.0.0.1:{server.config.port}"
    loaded = load_artifact(ARTIFACT)
    artifact = loaded.model_copy(update={"status": "approved", "policy": loaded.policy.model_copy(
        update={"allowed_origins": [base]})})

    (x, y, w, h), _ = asyncio.run(window(base))
    OUT.mkdir(parents=True, exist_ok=True)
    device = screen_device()
    scale = capture_scale(device)
    crop = f"crop={round(w * scale)}:{round(h * scale)}:{round(x * scale)}:{round(y * scale)}"
    recorder = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "avfoundation", "-capture_cursor", "1", "-framerate", "15",
         "-pixel_format", "nv12", "-i", f"{device}:none", "-vf", f"{crop},scale=960:-2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(OUT / "demo.mp4")], stdin=subprocess.PIPE)
    time.sleep(1.5)
    results = []
    try:
        for what, member, bank, fault in RUNS:
            BANK.fault(None)
            BANK.variant(bank)
            if fault:
                BANK.fault(fault)
            started = time.monotonic()
            result = asyncio.run(Replayer(artifact, base, {"memberId": member}, env=ENV, tenant=bank if bank != "a"
                                          else None, audit=Audit(OUT / "audit.jsonl"),
                                          pending_dir=OUT / "pending").run())
            results.append({"shows": what, "bank": bank.upper(), "fault": fault, "result": label(result),
                            "drift": [f"{d.step_id}:{d.strategy}" for d in getattr(result, "drift", [])],
                            "seconds": round(time.monotonic() - started, 1), "run_id": result.run_id})
            print(json.dumps(results[-1]), flush=True)
    finally:
        BANK.fault(None)
        BANK.variant("a")
        recorder.communicate(b"q", timeout=30)
        server.should_exit = True
    (OUT / "audit.jsonl").unlink(missing_ok=True)
    for pending in (OUT / "pending").glob("*"):
        pending.unlink()
    (OUT / "pending").rmdir() if (OUT / "pending").exists() else None
    (OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    rows = "\n".join(f"| {r['shows']} | {r['bank']} | `{r['result']}` | {', '.join(r['drift']) or '–'} | "
                     f"{r['seconds']} s | `{r['run_id']}` |" for r in results)
    (OUT / "README.md").write_text(f"""# Native window, real OS input

The artifact Claude recorded on the canvas client (`{ARTIFACT.relative_to(ROOT)}`), replayed with no model on a
native Tk window (`apps/mock_gui/desktop.py`): the screen read by OCR from a screen capture, the mouse and keyboard
driven through the operating system. Same artifact, a different renderer: the Tk window draws the same screens with
its own fonts, at the display's own scaling. `demo.mp4` is the window's rectangle only, recorded while it ran.

| shows | bank | result | drift | time | run |
|---|---|---|---|---|---|
{rows}

The video shows the mock's fake members unmasked; each run's evidence screenshots (under `runs/`) black out every
value. Regenerate with `uv run python scripts/desktop_demo.py` (macOS, with Screen Recording and Accessibility
permission for the terminal).
""")
    print(OUT / "demo.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
