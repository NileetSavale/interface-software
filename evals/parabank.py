"""Suite 6, replay stability on an app we didn't write: ParaBank, Parasoft's public demo bank. Needs the network.

The two approved ParaBank artifacts (artifacts/parabank.*.json), replayed with no model on both accounts of a fresh
fictional customer, N times each, spaced out: it's someone else's free server. The same questions as suite 3: does
every run succeed, does every run on an account read the same values, how long does a run take.

What only a public server does: it resets its database now and then, which deletes the customer. When a run fails at
sign-on, the login is tried outside replay: only if ParaBank says the customer doesn't exist is it a reset, counted
and reported, with a new customer registered and that run retried once. Any other failure counts as a failure.
"""
import asyncio
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from evals.common import ROOT, label, percentile
from interface.artifact.io import load_artifact
from interface.evidence import Audit
from interface.replay.executor import Replayer

sys.path.insert(0, str(ROOT / "scripts"))
from parabank_setup import BASE, TRANSFER, customer_exists, register  # noqa: E402

ARTIFACTS = {"account_balance": ROOT / "artifacts" / "parabank.account_balance.json",
             "find_transaction": ROOT / "artifacts" / "parabank.find_transaction_by_amount.json"}
PAUSE_S = 2.0  # between runs: a free public server


def inputs(flow: str, account: str) -> dict[str, str]:
    return {"accountId": account, **({"amount": TRANSFER} if flow == "find_transaction" else {})}


async def once(artifact, flow: str, account: str, audit: Audit):
    started = time.monotonic()
    result = await Replayer(artifact, BASE, inputs(flow, account), audit=audit).run()
    return result, time.monotonic() - started


def signed_out(result) -> bool:
    return result.status == "failed" and getattr(result, "at_step", "") in ("log_in", "enter_username", "enter_password")


async def main(n: int) -> dict:
    artifacts = {flow: load_artifact(path) for flow, path in ARTIFACTS.items()}
    audit = Audit(Path(__import__("tempfile").mkdtemp()) / "audit.jsonl")
    accounts = await register()
    resets = 0
    arms = []
    for flow, artifact in artifacts.items():
        labels, seconds, outputs = Counter(), [], defaultdict(Counter)
        for i in range(n):
            for slot in (0, 1):  # both accounts, alternating
                result, took = await once(artifact, flow, accounts[slot], audit)
                if signed_out(result) and not await customer_exists():
                    # The customer may be gone: a reset of the public database, not a replay fault
                    accounts = await register()
                    resets += 1
                    result, took = await once(artifact, flow, accounts[slot], audit)
                labels[label(result)] += 1
                seconds.append(took)
                if result.status == "success":
                    outputs[accounts[slot]][repr(sorted(result.outputs.items()))] += 1
                print(f"parabank {flow:<17} {i + 1:>3}/{n} account {slot + 1} {label(result)} {took:.1f}s", flush=True)
                await asyncio.sleep(PAUSE_S)
        runs = 2 * n
        arms.append({
            "flow": flow, "n": runs, "success_rate": labels["success"] / runs, "results": dict(labels),
            # Per account: every successful run on it should read the very same values
            "distinct_outputs": max((len(c) for c in outputs.values()), default=0),
            "p50_s": round(percentile(seconds, 50), 2), "p95_s": round(percentile(seconds, 95), 2),
            "max_s": round(max(seconds), 2),
        })
    return {"n_per_account": n, "site": BASE, "database_resets": resets, "arms": arms}


def run(n: int = 10, **_) -> dict:
    return asyncio.run(main(n))
