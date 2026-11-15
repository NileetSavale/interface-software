"""What every suite shares: its own mock app, the artifacts under test, a replay helper and the results file.

Each suite starts a private copy of the mock app on a free port, so its faults and layout modes can't disturb a dev
server on :4100 (or another suite). Faults are armed per request and used up by one page, so runs are sequential
and exactly repeatable.
"""
import asyncio
import json
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

from interface.artifact.io import load_artifact
from interface.evidence import RUNS, Audit
from interface.replay.executor import Replayer

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
ENV = {"APP_USER": "teller01", "APP_PASSWORD": "demo-only-password"}  # the mock app's demo operator, not a real one
ARTIFACTS = {
    "lookup": ROOT / "artifacts" / "core_banking.lookup_member_balance.json",
    "open_sub_account": ROOT / "artifacts" / "core_banking.open_sub_account.json",
}
HAPPY = {
    "lookup": {"memberId": "34567"},
    "open_sub_account": {"memberId": "34567", "accountType": "Holiday Club", "nickname": "Trip fund",
                         "initialDeposit": "300.00"},
}


def artifact(flow: str, app: "MockApp"):
    """The discovered artifact, approved in memory only (the file on disk keeps the status a reviewer gave it), with
    its allowed origin moved to this suite's private app, as a deployment's config would."""
    loaded = load_artifact(ARTIFACTS[flow])
    policy = loaded.policy.model_copy(update={"allowed_origins": [app.base_url]})
    return loaded.model_copy(update={"status": "approved", "policy": policy})


@contextmanager
def mock_app():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config("apps.mock_core_banking.app:app", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    app = MockApp(f"http://127.0.0.1:{port}")
    try:
        yield app
    finally:
        app.reset()
        server.should_exit = True


class MockApp:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def fault(self, name: str | None, times: int = 1):
        httpx.delete(self.base_url + "/__faults")
        if name:
            httpx.post(self.base_url + "/__faults", params={"name": name, "times": times}).raise_for_status()

    def layout(self, seed: int | None):
        """Seeded shuffle of layout, row and column order, and frame order; None puts the original back."""
        if seed is None:
            httpx.delete(self.base_url + "/__layout")
        else:
            httpx.post(self.base_url + "/__layout", params={"seed": seed}).raise_for_status()

    def variant(self, name: str):
        """Which bank this app plays: a (where the artifacts were recorded) or b (§7, variant B)."""
        httpx.post(self.base_url + "/__variant", params={"name": name}).raise_for_status()

    def confirmed(self) -> int:
        return httpx.get(self.base_url + "/__confirmed").json()["count"]

    def reset(self):
        self.fault(None)
        self.layout(None)
        self.variant("a")


def bank_c_map():
    """Bank C's page map, learned from one verified run of the lookup flow on C (a scripted discovery, so no model):
    the knowledge that C's menu says "Member Lookup". The caller's app must be playing bank C."""
    from interface.pagemap import PageMap
    from tests.test_pagemap import lookup_script, run_discovery
    bank = PageMap(bank="c")
    return bank, lookup_script(member_ref=r'cell "Member #"[^\n]*\n[^\n]*\n\s*- textbox', search=r'button "Find Member"',
                               menu=r'link "Member Lookup"'), run_discovery


def replay(app: MockApp, artifact, inputs: dict, replayer=Replayer, **kw):
    """One unattended replay with a throwaway audit log, so a wrong-record case can't trip the breaker for the next."""
    audit = Audit(Path(tempfile.mkdtemp()) / "audit.jsonl")
    started = time.monotonic()
    runner = replayer(artifact, app.base_url, inputs, env=ENV, audit=audit, **kw)
    result = asyncio.run(runner.run())
    return result, time.monotonic() - started, runner


def events(run_id: str) -> list[dict]:
    path = RUNS / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def label(result) -> str:
    """status/code: the result type plus what made it that, e.g. failed/WRONG_RECORD or business_outcome/X."""
    match result.status:
        case "success":
            return "success" + ("+recovered" if result.recovered else "") + ("+drift" if result.drift else "")
        case "business_outcome":
            return f"business_outcome/{result.code}"
        case "failed":
            return f"failed/{result.outcome_code or result.error_class}"
        case _:
            return result.status


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1]


def save(suite: str, data: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    # "+dirty" when the code under test has uncommitted changes, so a number is never tied to the wrong commit
    commit = git("rev-parse", "--short", "HEAD") + ("+dirty" if git("status", "--porcelain", "src", "apps", "evals", ":!evals/results", ":!evals/report.md") else "")
    data = {"suite": suite, "date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "commit": commit, **data}
    path = RESULTS / f"{suite}.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path
