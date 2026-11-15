"""Suite 3, replay stability: the same artifact, the same inputs, N times, with and without a slow app.

Deterministic replay should give the same result every time. A run that differs from the others, a step that needed
a second attempt or a fallback strategy, or a long tail in duration is the flakiness this suite is here to find.
No LLM.
"""
from collections import Counter

from evals.common import HAPPY, artifact, events, label, mock_app, percentile, replay, save

CONDITIONS = [("normal", None), ("slow app (+3 s on the member page)", "slow")]


def run(n: int = 20, **_) -> dict:
    arms = []
    with mock_app() as app:
        for flow in ("lookup", "open_sub_account"):
            for condition, fault in CONDITIONS:
                labels, seconds, outputs, unsteady = Counter(), [], Counter(), Counter()
                for i in range(n):
                    app.fault(fault)
                    result, took, _ = replay(app, artifact(flow, app), HAPPY[flow])
                    labels[label(result)] += 1
                    seconds.append(took)
                    if result.status == "success":
                        outputs[repr(sorted(result.outputs.items()))] += 1
                    for e in events(result.run_id):
                        # A step that only worked on a later strategy, or after its element went stale and was found again
                        if e["event"] == "drift":
                            unsteady[f"{e['step']} (fallback rank {e['rank']})"] += 1
                        if e["event"] == "recovered":
                            unsteady[f"{e['step']} (recovered {e['code']})"] += 1
                    print(f"{flow:<17} {condition:<36} {i + 1:>3}/{n} {label(result)} {took:.1f}s", flush=True)
                arms.append({
                    "flow": flow, "condition": condition, "n": n,
                    "success_rate": labels["success"] / n, "results": dict(labels),
                    # Every successful run should read the very same values
                    "distinct_outputs": len(outputs),
                    "p50_s": round(percentile(seconds, 50), 2), "p95_s": round(percentile(seconds, 95), 2),
                    "max_s": round(max(seconds), 2), "unsteady_steps": dict(unsteady),
                })
        assert app.confirmed() == 0
    return {"n_per_arm": n, "arms": arms}


if __name__ == "__main__":
    save("stability", run())
