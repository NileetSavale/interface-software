"""The run log: one JSON line per event, plus screenshots, snapshots and a Playwright trace, all under runs/<run_id>/.

Everything passes through redact() on the way to disk, which is the only place run data leaves memory: first the
values known to be sensitive (sensitive inputs, secrets, extracted outputs) become their placeholders, then the PII
detectors (interface.pii) mask whatever else looks like an account number, an amount, an SSN… Snapshots also get the
label detectors, which need the page's structure: the cell after "Name:" is a name.
"""
import json
import re
import secrets
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from interface.pii import Masker

RUNS = Path("runs")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id(kind: str) -> str:
    return f"{kind}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(3)}"


class RunLog:
    def __init__(self, run_id: str, masked: dict[str, str] | None = None, root: Path = RUNS):
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = (self.dir / "events.jsonl").open("a")
        # value -> what to show instead; longest first so a value that contains another is masked whole
        self._masks = sorted(((v, k) for v, k in (masked or {}).items() if v), key=lambda p: -len(p[0]))
        self._start = time.monotonic()
        self._pii = Masker()
        self.last: dict = {}  # the last event's kind and step or turn: where a crash happened

    def mask(self, value: str, label: str):
        if value:
            self._masks = sorted([*self._masks, (value, label)], key=lambda p: -len(p[0]))

    def redact(self, text: str, snapshot: bool = False) -> str:
        for value, label in self._masks:
            text = text.replace(value, label)
        return self._pii.snapshot(text) if snapshot else self._pii.text(text)

    def event(self, kind: str, /, **data):
        record = {"t": round(time.monotonic() - self._start, 3), "at": now(), "event": kind, **data}
        self.last = {k: record[k] for k in ("event", "step", "turn") if k in record}
        self._file.write(self.redact(json.dumps(record, default=str)) + "\n")
        self._file.flush()

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_text(self.redact(text, snapshot=name.endswith(".snapshot.txt")))
        return str(path)

    def path(self, name: str) -> str:
        return str(self.dir / name)

    def crashed(self, error: BaseException, **where) -> str:
        """An exception nothing planned for: what it was, where the run had got to, and the traceback in
        traceback.txt, all redacted. Returns the one-line reason."""
        reason = self.redact(f"{type(error).__name__}: {error}")[:500]
        self.write("traceback.txt", "".join(traceback.format_exception(error)))
        self.event("crashed", **where, error=reason, after=self.last, traceback="traceback.txt", **api_detail(error))
        return reason

    def close(self):
        if not self._file.closed:
            self._file.close()


def timeline(run_dir: Path) -> str:
    """A run's events.jsonl as one line per event, for a person checking a run or chasing a failure. The log is
    already redacted, so this shows nothing the file doesn't."""
    lines = [f"{run_dir.name}  ({run_dir})"]
    for raw in (run_dir / "events.jsonl").read_text().splitlines():
        record = json.loads(raw)
        if record["event"] == "model_turn":  # the tools called and the cost say more than the full inputs
            record = {**record, "calls": [c["tool"] for c in record.get("calls", [])], "usage": None}
        fields = " ".join(f"{k}={v if isinstance(v, (str, int, float)) else json.dumps(v)}"
                          for k, v in record.items() if k not in ("t", "at", "event") and v not in (None, [], {}, ""))
        lines.append(f"{record['t']:>8.2f}s  {record['event']:<20} {fields[:160]}")
    if (trace := run_dir / "traceback.txt").exists():
        lines += ["", "crashed; traceback (redacted):", trace.read_text().rstrip()]
    elif not lines[-1].split()[1].endswith("_finished"):
        lines += ["", "no *_finished event: the run is still going, or the process died before it could log"]
    return "\n".join(lines)


def api_detail(error: BaseException) -> dict:
    """What Anthropic support or a status page needs to find a failed model call: its HTTP status and request id."""
    return {k: v for k in ("status_code", "request_id") if (v := getattr(error, k, None)) is not None}


class Audit:
    """Security events that outlive a run, one JSON line each in runs/audit.jsonl, and the circuit breaker built on
    them. A WRONG_RECORD means the app served someone else's data: twice within the window and the capability stops
    running unattended until a person re-approves it (which writes a reset)."""

    TRIP_AFTER = 2
    WINDOW_S = 24 * 3600

    def __init__(self, path: Path = RUNS / "audit.jsonl"):
        self.path = path

    def record(self, event: str, capability: str, **data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"at": now(), "epoch": time.time(), "event": event, "capability": capability, **data}) + "\n")

    def tripped(self, capability: str) -> bool:
        """Whether the breaker is open: enough wrong records since the last reset, inside the window."""
        if not self.path.exists():
            return False
        recent = []
        for line in self.path.read_text().splitlines():
            entry = json.loads(line)
            if entry["capability"] != capability:
                continue
            if entry["event"] == "breaker_reset":
                recent = []
            elif entry["event"] == "wrong_record" and time.time() - entry["epoch"] < self.WINDOW_S:
                recent.append(entry)
        return len(recent) >= self.TRIP_AFTER


# A value-bearing element ("- cell \"Jane Doe\"", YAML-quoted when it holds a #) and a bare text node ("- text: …")
_VALUE_LINE = re.compile(r'^(\s*- \'?(?:cell|text|generic|paragraph|rowheader|strong|emphasis|listitem) )"((?:[^"\\]|\\.)*)"')
_TEXT_LINE = re.compile(r'^(\s*- text: )(.*)$')


def structure_only(snapshot: str) -> str:
    """A snapshot with the record's data taken out, for evidence of a screen that showed someone else's record: every
    digit, and the text of every value cell, are masked. Labels ("Name:"), headings, columns and controls stay, so the
    page is still recognisable."""
    def mask(match: re.Match) -> str:
        text = match.group(2)
        return match.group(0) if text.rstrip().endswith(":") else f'{match.group(1)}"▒"'
    def mask_text(match: re.Match) -> str:
        return match.group(0) if match.group(2).strip().strip("\"").endswith(":") else f"{match.group(1)}▒"
    lines = (_TEXT_LINE.sub(mask_text, _VALUE_LINE.sub(mask, line)) for line in snapshot.splitlines())
    return re.sub(r"\d", "#", "\n".join(lines))
