"""The run log: one JSON line per event, plus screenshots, snapshots and a Playwright trace, all under runs/<run_id>/.

Everything passes through redact() on the way to disk, which is the only place run data leaves memory. Pass 1 masks
the values it knows are sensitive (sensitive inputs and secrets); pattern-based PII detection comes in Pass 2.
"""
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

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

    def mask(self, value: str, label: str):
        if value:
            self._masks = sorted([*self._masks, (value, label)], key=lambda p: -len(p[0]))

    def redact(self, text: str) -> str:
        for value, label in self._masks:
            text = text.replace(value, label)
        return text

    def event(self, kind: str, /, **data):
        record = {"t": round(time.monotonic() - self._start, 3), "at": now(), "event": kind, **data}
        self._file.write(self.redact(json.dumps(record, default=str)) + "\n")
        self._file.flush()

    def write(self, name: str, text: str) -> str:
        path = self.dir / name
        path.write_text(self.redact(text))
        return str(path)

    def path(self, name: str) -> str:
        return str(self.dir / name)

    def close(self):
        self._file.close()
