"""Logs for the long-running processes: the MCP server and the two mock banks. A run's own story is in
runs/<run_id>/events.jsonl (evidence.py); this is the process around it: what started, who connected, which calls
came in and how they ended, which faults fired, and anything that went wrong between runs.

One JSON line per event in runs/<name>.log (rotated at 1 MB, 3 kept), through the same PII masker as the run logs.
Fields are chosen to carry no values in the first place (input names, not inputs; key names, not keystrokes); the
masker is the second line. Echoed to stderr when the process is run from the CLI, never to stdout: the MCP server's
stdout is its protocol channel.
"""
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from interface.evidence import RUNS, now
from interface.pii import Masker


class _Line(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class ServerLog:
    def __init__(self, name: str, root: Path = RUNS):
        self.name = name
        self._log = logging.getLogger(f"interface.{name}")
        self._log.setLevel(logging.INFO)
        self._log.propagate = False
        if not self._log.handlers:  # one handler per process, however many times it's asked for
            root.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(root / f"{name}.log", maxBytes=1_000_000, backupCount=3)
            handler.setFormatter(_Line())
            self._log.addHandler(handler)
        self._pii = Masker()

    def echo(self) -> "ServerLog":
        """Also to stderr, as the same JSON lines: for a process a person started and is watching."""
        if not any(getattr(h, "stream", None) is sys.stderr for h in self._log.handlers):
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(_Line())
            self._log.addHandler(handler)
        return self

    def event(self, kind: str, /, *, level: int = logging.INFO, **data):
        line = json.dumps({"at": now(), "server": self.name, "event": kind, **data}, default=str)
        self._log.log(level, self._pii.text(line))

    def error(self, kind: str, /, **data):
        self.event(kind, level=logging.ERROR, **data)
