"""Suite 5, recorder robustness: do the recorded descriptors still find the same control when the layout moves?

The mock app's layout mode reorders frames, fieldsets, table rows and columns and adds nesting, seeded, while every
label stays beside its field. Seed 0 is the original layout: a baseline run there records which control (by its
data-truth marker, invisible to the recorder) each step resolves to. Every shuffled seed is then replayed and each
step's resolution compared with it:

    same       found the same control (and at which strategy rank)
    WRONG      found a different control: the failure that matters, since the run may carry on regardless
    unresolved no strategy found exactly one match (the run stops there, which is safe)
    not run    an earlier step stopped the run

No LLM.
"""
from collections import Counter, defaultdict

from evals.common import HAPPY, artifact, label, mock_app, replay, save
from interface.replay.executor import Replayer

TRUTH_JS = "el => el.closest('[data-truth]')?.getAttribute('data-truth') ?? null"


class Tracing(Replayer):
    """A replay that notes which marked control each step resolved to, and by which strategy."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.trace: dict[str, tuple[str | None, int, str]] = {}

    async def _resolve(self, step, target, timeout, recovering):
        hit = await super()._resolve(step, target, timeout, recovering)
        self.trace[step.id] = (await hit.handle.evaluate(TRUTH_JS), hit.rank, hit.strategy)
        return hit


def run(n: int = 30, **_) -> dict:
    flows = []
    with mock_app() as app:
        for flow in ("lookup", "open_sub_account", "find_member", "history"):
            subject = artifact(flow, app)
            steps = [s.id for s in subject.all_steps() if s.action != "navigate"]
            app.layout(0)
            base, _, base_run = replay(app, subject, HAPPY[flow], replayer=Tracing)
            assert base.status == "success", base
            baseline = {step: truth for step, (truth, _, _) in base_run.trace.items()}
            per_step: dict[str, Counter] = defaultdict(Counter)
            ranks: dict[str, Counter] = defaultdict(Counter)
            results, wrong_but_succeeded = Counter(), 0
            for seed in range(1, n + 1):
                app.layout(seed)
                result, _, runner = replay(app, subject, HAPPY[flow], replayer=Tracing)
                results[label(result)] += 1
                stopped_at = getattr(result, "at_step", None)
                wrong = False
                for step in steps:
                    if step in runner.trace:
                        truth, rank, strategy = runner.trace[step]
                        verdict = "same" if truth == baseline.get(step) else "WRONG"
                        wrong |= verdict == "WRONG"
                        ranks[step][f"{rank}:{strategy}"] += 1
                    else:
                        verdict = "unresolved" if step == stopped_at else "not run"
                    per_step[step][verdict] += 1
                if result.status == "success" and (wrong or result.outputs != base.outputs):
                    wrong_but_succeeded += 1
                print(f"{flow:<17} seed {seed:>3}  {label(result)}" + ("  WRONG CONTROL" if wrong else ""), flush=True)
            flows.append({
                "flow": flow, "seeds": n, "results": dict(results),
                "success_rate": results["success"] / n,
                # A run that reports success with the wrong control or wrong values: must be zero
                "silent_wrong": wrong_but_succeeded,
                "steps": [{"step": s, "baseline": baseline.get(s), **{k: per_step[s][k] for k in
                           ("same", "WRONG", "unresolved", "not run")}, "ranks": dict(ranks[s])} for s in steps],
            })
        app.layout(None)
        assert app.confirmed() == 0
    return {"seeds": n, "flows": flows}


if __name__ == "__main__":
    save("robustness", run())
