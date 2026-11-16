"""Suite 8, the green screen: does the same thread hold on a character terminal? (TerminalSurface, D31)

On a private copy of the mock terminal host (apps/mock_terminal), with the artifact Claude recorded there
(artifacts/core_banking_terminal.lookup_member_balance.json):

1. Fault matrix, no LLM: every outcome and fault the host has, on bank A, on bank B at first contact (relabelled,
   same grid) and on B with the override that contact proposed, approved.
2. Commit guard, no LLM: a surface signed on as an operator walks to the sub-account review screen and presses
   F10=Post; the key must never be sent, and no sub-account is opened.
3. Stability, no LLM: N replays on rotating members; one distinct output per member, timings.
4. Discovery, Claude (--llm): N real discoveries from the goal, each replayed on another member. Spends money.
"""
import asyncio
import json
import os
import socket
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from apps.mock_terminal.app import TerminalBank
from evals.common import ENV, ROOT, events, label, percentile
from interface.artifact.io import load_artifact
from interface.artifact.schema import Override, Policy
from interface.evidence import Audit
from interface.replay.executor import Replayer
from interface.surface.terminal import TerminalSurface

ARTIFACT = ROOT / "artifacts" / "core_banking_terminal.lookup_member_balance.json"
GOAL = ROOT / "goals" / "terminal_lookup_member_balance.json"
HAPPY = {"memberId": "34567"}
CASES = [  # (name, inputs, fault, expected label)
    ("happy", HAPPY, None, "success"),
    ("not_found", {"memberId": "99999"}, None, "business_outcome/MEMBER_NOT_FOUND"),
    ("malformed", {"memberId": "01234"}, None, "business_outcome/INVALID_MEMBER_NUMBER"),
    ("restricted", {"memberId": "45678"}, None, "business_outcome/PERMISSION_DENIED"),
    ("bad_input", {"memberId": "1234"}, None, "invalid_input"),
    ("session_timeout", HAPPY, "session_timeout", "success+recovered"),
    ("server_error", HAPPY, "server_error", "failed/APP_ERROR"),
    ("wrong_record", HAPPY, "wrong_record", "failed/WRONG_RECORD"),
    ("slow", HAPPY, "slow", "success"),
    ("late_fill", HAPPY, "late_fill", "success"),
]
MEMBERS = {"12345": "Jane Doe", "23456": "Rafael Ortiz", "34567": "Mei Chen", "56789": "Ana Chen"}


class Host:
    def __init__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.bank = TerminalBank().start_in_thread(port)
        self.base_url = f"telnet://127.0.0.1:{port}"

    def close(self):
        self.bank.loop.call_soon_threadsafe(self.bank.server.close)


def approved(host: Host):
    loaded = load_artifact(ARTIFACT)
    policy = loaded.policy.model_copy(update={"allowed_origins": [host.base_url]})
    return loaded.model_copy(update={"status": "approved", "policy": policy})


def replay(host: Host, artifact, inputs: dict, fault: str | None = None, variant: str = "a", **kw):
    host.bank.fault(None)
    host.bank.variant(variant)
    if fault:
        host.bank.fault(fault)
    started = time.monotonic()
    try:
        result = asyncio.run(Replayer(artifact, host.base_url, inputs, env=ENV,
                                      audit=Audit(Path(tempfile.mkdtemp()) / "a.jsonl"), **kw).run())
    finally:
        host.bank.fault(None)
        host.bank.variant("a")
    return result, time.monotonic() - started


def fault_matrix(host: Host) -> list[dict]:
    base = approved(host)
    pending = Path(tempfile.mkdtemp())
    first, _ = replay(host, base, HAPPY, variant="b", tenant="b", pending_dir=pending)
    override = Override.model_validate_json((pending / "b.json").read_text())
    with_override = base.model_copy(update={"overrides": {"b": override}})
    rows = []
    for arm, subject, variant, tenant in (("A", base, "a", None), ("B first contact", base, "b", "b"),
                                          ("B override approved", with_override, "b", "b")):
        for name, inputs, fault, expected in CASES:
            want = expected + ("+drift" if arm == "B first contact" and expected.startswith("success") else "")
            result, took = replay(host, subject, inputs, fault, variant, tenant=tenant,
                                  pending_dir=Path(tempfile.mkdtemp()))
            got = label(result)
            rows.append({"arm": arm, "case": name, "expected": want, "got": got, "pass": got == want,
                         "drift": [f"{d.step_id}:{d.strategy}" for d in getattr(result, "drift", [])],
                         "seconds": round(took, 2), "run_id": result.run_id})
            print(f"terminal {arm:<20} {name:<16} {got:<40} {'ok' if got == want else 'FAIL want ' + want}", flush=True)
    rows.append({"arm": "B first contact", "case": "override proposed", "expected": "two ops", "got":
                 ", ".join(f"{op.step}" for op in override.ops), "pass": len(override.ops) >= 1, "drift": [],
                 "seconds": 0, "run_id": first.run_id})
    return rows


def commit_guard(host: Host) -> dict:
    async def walk():
        surface = TerminalSurface(policy=Policy(allowed_origins=[host.base_url], commit_paths=["/CB0410"]))
        await surface.navigate(host.base_url + "/")

        async def do(role, name, value=None):
            await surface.observe()
            ref = next(r for r in surface._refs if surface.ref_line(r) == (role, name))
            await surface.act_on_ref(ref, "type" if value is not None else "click", value)
        await do("textbox", "User ID", ENV["APP_USER"])
        await do("textbox", "Password", ENV["APP_PASSWORD"])
        await do("button", "Sign on")
        await do("textbox", "Option", "2")
        await do("button", "Select")
        for name, value in (("Member #", "12345"), ("Account type", "Holiday Club"), ("Nickname", "Trip fund"),
                            ("Initial deposit", "300.00")):
            await do("textbox", name, value)
        await do("button", "Review")
        on = (await surface.observe()).url.rsplit("/", 1)[-1]
        await do("button", "Post")
        after = (await surface.observe()).url.rsplit("/", 1)[-1]
        await surface.close()
        return on, after, list(surface.blocked)

    before = host.bank.confirmed
    on, after, blocked = asyncio.run(walk())
    opened = host.bank.confirmed - before
    print(f"terminal commit guard: on {on}, F10 pressed, still on {after}, blocked {len(blocked)}, opened {opened}",
          flush=True)
    return {"review_screen": on, "after_post": after, "blocked": blocked, "sub_accounts_opened": opened,
            "pass": on == "CB0410" and after == "CB0410" and len(blocked) == 1 and opened == 0}


def stability(host: Host, n: int) -> dict:
    subject = approved(host)
    labels, seconds, outputs = Counter(), [], defaultdict(Counter)
    for i in range(n):
        member = list(MEMBERS)[i % len(MEMBERS)]
        result, took = replay(host, subject, {"memberId": member})
        labels[label(result)] += 1
        seconds.append(took)
        if result.status == "success":
            outputs[member][json.dumps(result.outputs, sort_keys=True)] += 1
            assert result.outputs["memberName"] == MEMBERS[member], result.outputs
    print(f"terminal stability: {dict(labels)}, p50 {percentile(seconds, 50):.2f}s", flush=True)
    return {"n": n, "results": dict(labels), "success_rate": labels["success"] / n,
            "distinct_outputs_per_member": max((len(c) for c in outputs.values()), default=0),
            "p50_s": round(percentile(seconds, 50), 2), "p95_s": round(percentile(seconds, 95), 2)}


def discovery(host: Host, n: int, budget: float) -> dict:
    from interface.discovery.loop import discover
    goal = json.loads(GOAL.read_text())
    runs, spent = [], 0.0
    for i in range(n):
        if spent + 0.10 > budget:
            print(f"budget: ${spent:.2f} of ${budget:.2f} spent, stopping", flush=True)
            break
        out_dir = Path(tempfile.mkdtemp())
        outcome = asyncio.run(discover(goal, host.base_url, {"memberId": "12345"}, out_dir=out_dir,
                                       budget_usd=min(1.0, budget - spent), env=ENV))
        spent += outcome.get("cost_usd", 0.0)
        replayed = None
        if outcome["status"] == "recorded":
            recorded = load_artifact(outcome["artifact"]).model_copy(update={"status": "approved"})
            result, _ = replay(host, recorded, HAPPY)
            replayed = label(result)
        log = events(outcome["run_id"])
        runs.append({"run": i + 1, "status": outcome["status"], "replay_other_member": replayed,
                     "turns": sum(e["event"] == "model_turn" for e in log), "cost_usd": outcome.get("cost_usd"),
                     "escalations": sum(e["event"] == "escalated" for e in log),
                     "probes": [p["result"] for p in outcome.get("probes", [])], "run_id": outcome["run_id"]})
        print(f"terminal discovery {i + 1}/{n} {outcome['status']} replay={replayed} turns={runs[-1]['turns']} "
              f"${outcome.get('cost_usd', 0):.3f}", flush=True)
    costs = [r["cost_usd"] or 0 for r in runs]
    return {"n": len(runs), "recorded": sum(r["status"] == "recorded" for r in runs),
            "replays_on_other_member": sum((r["replay_other_member"] or "").startswith("success") for r in runs),
            "median_turns": statistics.median([r["turns"] for r in runs]) if runs else None,
            "median_cost_usd": round(statistics.median(costs), 4) if costs else None,
            "spent_usd": round(spent, 4), "escalated_runs": sum(r["escalations"] > 0 for r in runs), "runs": runs}


def run(n: int = 20, llm: bool = False, budget: float = 2.0, discovery_n: int = 5, **_) -> dict:
    host = Host()
    try:
        rows = fault_matrix(host)
        guard = commit_guard(host)
        stable = stability(host, n)
        found = None
        if llm:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit("--llm calls Claude: set ANTHROPIC_API_KEY (in .env)")
            found = discovery(host, discovery_n, budget)
        opened = host.bank.confirmed
    finally:
        host.close()
    return {"fault_matrix": rows, "passed": sum(r["pass"] for r in rows), "cases": len(rows), "commit_guard": guard,
            "stability": stable, "discovery": found, "sub_accounts_opened": opened}
