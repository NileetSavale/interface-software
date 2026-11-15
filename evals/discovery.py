"""Suite 2, discovery reliability: real Claude runs, N per goal, under a hard USD budget for the whole suite.

Per run: did it record an artifact, does that artifact replay for a different member (a recording that only works
for the member it was made on isn't a capability), how many turns, what it cost, and how often the ladder was used
(stuck nudges, escalation to the stronger model, a person). The map-on arm and variant B arrive with the page map
and the second tenant (build items 6 and 7); until then this is the map-off, variant-A baseline.

Spends real money: needs ANTHROPIC_API_KEY. The budget is checked before every run and passed down as the run's own
cap, so the suite stops cleanly instead of overshooting.
"""
import asyncio
import json
import os
import statistics
import tempfile
from pathlib import Path

from evals.common import ENV, HAPPY, ROOT, events, label, mock_app, replay, save
from interfaze.artifact.io import load_artifact

GOALS = {"lookup": ROOT / "goals" / "lookup_member_balance.json",
         "open_sub_account": ROOT / "goals" / "open_sub_account.json"}
DISCOVERED_ON = {"memberId": "12345"}  # recorded on one member, replayed on another (HAPPY uses 34567)
PER_RUN_CAP = 1.00


class Budget:
    def __init__(self, usd: float):
        self.usd, self.spent, self.largest = usd, 0.0, 0.0

    def allows_another(self) -> bool:
        # Assume the next run costs as much as the dearest so far (a first guess of $0.10)
        return self.spent + max(self.largest, 0.10) <= self.usd

    def cap(self) -> float:
        return min(PER_RUN_CAP, self.usd - self.spent)

    def charge(self, usd: float):
        self.spent += usd
        self.largest = max(self.largest, usd)


def discover_once(app, goal: dict, inputs: dict, budget: Budget, fault: str | None = None, **kw) -> dict:
    from interfaze.discovery.loop import discover
    app.fault(fault, -1 if fault else 1)
    out_dir = Path(tempfile.mkdtemp())
    outcome = asyncio.run(discover(goal, app.base_url, inputs, out_dir=out_dir, budget_usd=budget.cap(), env=ENV, **kw))
    app.fault(None)
    budget.charge(outcome.get("cost_usd", 0.0))
    log = events(outcome["run_id"])
    return {"outcome": outcome, "events": log,
            "turns": sum(e["event"] == "model_turn" for e in log),
            "nudges": sum(e["event"] == "nudged" for e in log),
            "escalations": sum(e["event"] == "escalated" for e in log),
            "results": [e["result"] for e in log if e["event"] == "tool_result"],
            "errors": [e["result"] for e in log if e["event"] == "tool_result" and e["error"]]}


def run(n: int = 5, budget: float = 10.0, **_) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("suite 2 calls Claude: set ANTHROPIC_API_KEY (in .env)")
    spend = Budget(budget)
    arms = []
    with mock_app() as app:
        for flow, path in GOALS.items():
            goal = json.loads(path.read_text())
            inputs = {**HAPPY[flow], **DISCOVERED_ON}
            runs = []
            for i in range(n):
                if not spend.allows_another():
                    print(f"budget: ${spend.spent:.2f} of ${spend.usd:.2f} spent, stopping", flush=True)
                    break
                r = discover_once(app, goal, inputs, spend)
                outcome = r["outcome"]
                replayed = None
                if outcome["status"] == "recorded":
                    # Policy rebinds to this app's origin, as in the other suites
                    recorded = load_artifact(outcome["artifact"])
                    policy = recorded.policy.model_copy(update={"allowed_origins": [app.base_url]})
                    result, _, _ = replay(app, recorded.model_copy(update={"status": "approved", "policy": policy}),
                                          HAPPY[flow])
                    replayed = label(result)
                runs.append({"run": i + 1, "status": outcome["status"], "replay_other_member": replayed,
                             "steps": outcome.get("steps"), "turns": r["turns"], "cost_usd": outcome.get("cost_usd"),
                             "nudges": r["nudges"], "escalations": r["escalations"], "tool_errors": len(r["errors"]),
                             "probes": [p["result"] for p in outcome.get("probes", [])], "run_id": outcome["run_id"]})
                print(f"{flow:<17} {i + 1}/{n} {outcome['status']} replay={replayed} turns={r['turns']} "
                      f"${outcome.get('cost_usd', 0):.3f}", flush=True)
            costs = [r["cost_usd"] or 0 for r in runs]
            arms.append({
                "flow": flow, "variant": "a", "map": "off", "n": len(runs),
                "recorded": sum(r["status"] == "recorded" for r in runs),
                "replays_on_other_member": sum((r["replay_other_member"] or "").startswith("success") for r in runs),
                "median_turns": statistics.median([r["turns"] for r in runs]) if runs else None,
                "median_cost_usd": round(statistics.median(costs), 4) if costs else None,
                "total_cost_usd": round(sum(costs), 4),
                "nudged_runs": sum(r["nudges"] > 0 for r in runs),
                "escalated_runs": sum(r["escalations"] > 0 for r in runs),
                "runs": runs,
            })
        assert app.confirmed() == 0
    return {"n_per_arm": n, "budget_usd": budget, "spent_usd": round(spend.spent, 4), "arms": arms}


if __name__ == "__main__":
    save("discovery", run())
