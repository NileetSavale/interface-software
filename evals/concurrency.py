"""Suite 7, concurrent replays: what breaks when many run at once? No LLM.

Two banks, each its own mock-app server process (so neither shares the other's state): bank A on the base
artifact, bank B on the same artifact with its approved override and its own operator credentials (D26). Lookup
replays for five different members are run at concurrency 1, 4 and 8 in one process, each in its own browser.

Every result must be its own: a success carrying the name of the member it asked for. A shared cookie, session,
control token or output between runs would show as another member's name or a WRONG_RECORD, so either counts as a
failure here. Also measured: throughput (replays a minute) and per-run time at each level.
"""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from evals.common import ENV, ROOT, artifact, label, percentile
from interface.artifact.schema import Override
from interface.evidence import Audit
from interface.replay.executor import Replayer

# Members with a savings account, which lookup reads (Wei Chen has only checking: an honest TARGET_UNRESOLVED there,
# not what this suite is about)
MEMBERS = {"12345": "Jane Doe", "23456": "Rafael Ortiz", "34567": "Mei Chen", "56789": "Ana Chen"}
LEVELS = (1, 4, 8)


class Bank:
    """A mock-app server of one variant, in its own process."""

    def __init__(self, variant: str):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.variant, self.base_url = variant, f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen([sys.executable, "-m", "interface.cli", "mock-app", "--variant", variant,
                                      "--port", str(self.port)], cwd=ROOT, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": str(ROOT)})
        for _ in range(200):
            try:
                httpx.get(self.base_url + "/", timeout=0.5)
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError(f"bank {variant} didn't start")

    def close(self):
        self.proc.terminate()
        self.proc.wait(10)

    def confirmed(self) -> int:
        return httpx.get(self.base_url + "/__confirmed").json()["count"]


async def one(subject, bank: Bank, member: str, tenant: str | None):
    started = time.monotonic()
    audit = Audit(Path(tempfile.mkdtemp()) / "audit.jsonl")  # each run's own: one's incident can't stop another
    result = await Replayer(subject, bank.base_url, {"memberId": member}, env=ENV, audit=audit, tenant=tenant,
                            pending_dir=Path(tempfile.mkdtemp())).run()
    return result, member, time.monotonic() - started


async def level(jobs: list, width: int):
    gate = asyncio.Semaphore(width)

    async def gated(job):
        async with gate:
            return await one(*job)

    started = time.monotonic()
    results = await asyncio.gather(*(gated(j) for j in jobs))
    return results, time.monotonic() - started


def run(rounds: int = 3, **_) -> dict:
    banks = {v: Bank(v) for v in ("a", "b")}
    try:
        subjects = {v: artifact("lookup", b) for v, b in banks.items()}
        # Bank B on its approved override: learned from one first-contact replay, as in the fault matrix
        pending = Path(tempfile.mkdtemp())
        first = asyncio.run(Replayer(subjects["b"], banks["b"].base_url, {"memberId": "34567"}, env=ENV, tenant="b",
                                     audit=Audit(pending / "audit.jsonl"), pending_dir=pending).run())
        assert first.status == "success", first
        proposal = Override.model_validate_json((pending / "b.json").read_text())
        subjects["b"] = subjects["b"].model_copy(update={"overrides": {"b": proposal}})

        jobs = [(subjects[v], banks[v], m, v) for _ in range(rounds) for v in banks for m in MEMBERS]
        arms = []
        for width in LEVELS:
            results, wall = asyncio.run(level(jobs, width))
            labels, wrong, seconds = Counter(), 0, []
            for result, member, took in results:
                labels[label(result)] += 1
                seconds.append(took)
                own = result.status == "success" and result.outputs.get("memberName") == MEMBERS[member]
                wrong += result.status == "success" and not own  # someone else's answer, reported as success
            ok = sum(1 for r, m, _ in results if r.status == "success" and r.outputs.get("memberName") == MEMBERS[m])
            arms.append({"concurrency": width, "runs": len(results), "own_answer": ok, "someone_elses": wrong,
                         "results": dict(labels), "wall_s": round(wall, 1),
                         "per_minute": round(len(results) / wall * 60, 1),
                         "p50_s": round(percentile(seconds, 50), 2), "p95_s": round(percentile(seconds, 95), 2)})
            print(f"concurrency {width}: {ok}/{len(results)} own answers, {wrong} someone else's, "
                  f"{arms[-1]['per_minute']}/min, p95 {arms[-1]['p95_s']}s", flush=True)
        opened = sum(b.confirmed() for b in banks.values())
    finally:
        for b in banks.values():
            b.close()
    return {"banks": list(banks), "members": len(MEMBERS), "rounds": rounds, "levels": list(LEVELS), "arms": arms,
            "sub_accounts_opened": opened}
