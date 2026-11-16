"""The drift report: across many replays, which bank's steps have moved, and what a person should do about each.

One replay that finds a control by a fallback strategy succeeds and says so (§4 drift); it can't tell a bank that
changed a label last week from one flaky page load. This reads the run logs (runs/<run_id>/events.jsonl, already
redacted) and looks at each step of each capability on each bank over its most recent runs:

    stable            every recent run found the control by the strategy recorded first
    watch             a fallback now and then, not settled: nothing to approve yet
    approve override  the last few runs on a bank all found it by the same fallback, none failed: the bank changed,
                      and the override drift proposed for it is what to review (interface override … --tenant)
    re-record         the last few runs all missed the control (or what should follow it), the fallback keeps
                      changing, or the default version itself drifted (no tenant to override for): re-record the step

Only runs of the latest revision count (the steps a run executed, hashed): approving an override starts afresh.
It only reads: approving, re-recording and re-approving stay with a person, as everywhere else.
"""
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

FIRST = "first"  # found by the strategy recorded first: no drift
# A step that couldn't find its control, or didn't find on screen what it expected after acting
MISSES = ("TARGET_UNRESOLVED", "AMBIGUOUS_TARGET", "EXPECT_FAILED")
DEFAULT = "(default)"  # runs with no tenant


@dataclass
class RunSummary:
    run_id: str
    at: str
    capability: str  # id@version
    tenant: str
    status: str
    revision: str | None  # the steps it ran (an approved override or a re-recording is a new revision)
    # step -> how its control was found: FIRST, a fallback strategy's name, "pageMap", or "failed:<class>"
    steps: dict[str, str] = field(default_factory=dict)


@dataclass
class StepDrift:
    capability: str
    tenant: str
    step: str
    verdict: str
    reason: str
    runs: int  # recent runs that reached the step
    first: int  # of those, found by the first strategy
    fallbacks: dict[str, int]
    failures: dict[str, int]
    since: str | None = None  # the run from which every run on this step drifted
    action: str | None = None


def summarize(events: list[dict]) -> RunSummary | None:
    """One run's log, reduced to how each step's control was found. None for a run that never started properly."""
    start = next((e for e in events if e["event"] == "replay_started"), None)
    end = next((e for e in events if e["event"] == "replay_finished"), None)
    if start is None or end is None:
        return None
    run = RunSummary(run_id=end["result"].get("runId", ""), at=start["at"],
                     capability=f"{start['capability']}@{start['version']}", tenant=start.get("tenant") or DEFAULT,
                     status=end["status"], revision=start.get("revision"))
    for e in events:
        step = e.get("step")
        match e["event"]:
            case "drift":
                run.steps[step] = e["strategy"]
            case "acted":
                run.steps[step] = e["strategy"] if e.get("rank") else FIRST
            case "extracted" | "page_found":
                run.steps.setdefault(step, FIRST)
            case "map_fallback" if e.get("result") == "recovered":
                run.steps[step] = "pageMap"
    result = end["result"]
    if end["status"] == "failed" and result.get("errorClass") in MISSES:
        run.steps[result["atStep"]] = f"failed:{result['errorClass']}"
    return run


def read_runs(root: Path, run_ids: list[str] | None = None) -> list[RunSummary]:
    dirs = [root / r for r in run_ids] if run_ids is not None else sorted(root.glob("replay_*"))
    runs = []
    for d in dirs:
        path = d / "events.jsonl"
        if not path.exists():
            continue
        try:
            events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except json.JSONDecodeError:
            continue  # a run cut off mid-line
        if run := summarize(events):
            run.run_id = run.run_id or d.name
            runs.append(run)
    return runs


def _verdict(seen: list[tuple[str, str]], tenant: str, settle: int) -> tuple[str, str]:
    """seen: (run_id, how) for the recent runs that reached the step, oldest first."""
    hows = [h for _, h in seen]
    recent = hows[-settle:]
    if all(h == FIRST for h in hows):
        return "stable", "every recent run found it by the first strategy"
    # Only a settled change is acted on: a single miss can be the input (two members born the same day are
    # AMBIGUOUS_TARGET by design) or one bad page load
    if len(recent) == settle and FIRST not in recent:
        if all(h.startswith("failed:") for h in recent):
            classes = ", ".join(dict.fromkeys(h.removeprefix("failed:") for h in recent))
            return "re-record", f"the last {settle} runs all failed at this step ({classes})"
        kinds = set(recent)
        if len(kinds) > 1:
            return "re-record", f"no strategy holds: the last {settle} runs went {', '.join(recent)}"
        how = kinds.pop()
        if tenant == DEFAULT:
            return "re-record", (f"the last {settle} runs all needed '{how}', and with no tenant there's no override "
                                 f"to approve: the recorded version itself is out of date")
        return "approve override", f"the last {settle} runs all found it by '{how}', none failed"
    moved = sum(h != FIRST and not h.startswith("failed:") for h in hows)
    failed = sum(h.startswith("failed:") for h in hows)
    return "watch", f"{moved} fallback(s) and {failed} miss(es) in {len(hows)} recent runs, not settled"


def report(runs: list[RunSummary], window: int = 20, settle: int = 3, artifacts: Path | None = None) -> list[StepDrift]:
    by_bank: dict[tuple[str, str], list[RunSummary]] = defaultdict(list)
    for run in runs:
        by_bank[(run.capability, run.tenant)].append(run)
    paths = _artifact_paths(artifacts) if artifacts else {}
    rows = []
    for (capability, tenant), bank_runs in sorted(by_bank.items()):
        ordered = sorted(bank_runs, key=lambda r: (r.at, r.run_id))
        # Only runs of the latest revision: once an override is approved, the drift it fixed is history
        recent = [r for r in ordered if r.revision == ordered[-1].revision][-window:]
        steps = list(dict.fromkeys(s for r in recent for s in r.steps))
        for step in steps:
            seen = [(r.run_id, r.steps[step]) for r in recent if step in r.steps]
            verdict, reason = _verdict(seen, tenant, settle)
            hows = [h for _, h in seen]
            streak = next((i for i, h in enumerate(reversed(hows)) if h == FIRST), len(hows))  # trailing drifted runs
            since = seen[-streak][0] if streak else None
            rows.append(StepDrift(
                capability=capability, tenant=tenant, step=step, verdict=verdict, reason=reason, runs=len(seen),
                first=hows.count(FIRST), since=since,
                fallbacks={h: hows.count(h) for h in dict.fromkeys(hows) if h != FIRST and not h.startswith("failed:")},
                failures={h.removeprefix("failed:"): hows.count(h) for h in dict.fromkeys(hows) if h.startswith("failed:")},
                action=_action(verdict, capability, tenant, step, paths.get(capability))))
    return rows


def _artifact_paths(directory: Path) -> dict[str, Path]:
    paths = {}
    for path in directory.glob("*.json"):
        try:
            capability = json.loads(path.read_text())["capability"]
            paths[f"{capability['id']}@{capability['version']}"] = path
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
    return paths


def _action(verdict: str, capability: str, tenant: str, step: str, path: Path | None) -> str | None:
    artifact = str(path) if path else f"<the {capability.split('@')[0]} artifact>"
    if verdict == "approve override":
        pending = path.parent / capability.split("@")[0] / "overrides.pending" / f"{tenant}.json" if path else None
        if pending is not None and not pending.exists():
            return (f"no pending override on file for {tenant}: replay once with --tenant {tenant} to propose one, "
                    f"then: interface override {artifact} --tenant {tenant}")
        return f"review it: interface override {artifact} --tenant {tenant}, then add --approve"
    if verdict == "re-record":
        return (f"re-record step '{step}' (interface discover … --base-url <{tenant}'s app>), review and re-approve "
                f"{artifact}")
    return None


def render(rows: list[StepDrift], runs: list[RunSummary]) -> str:
    counts = defaultdict(int)
    for run in runs:
        counts[(run.capability, run.tenant)] += 1
    lines = []
    for (capability, tenant), n in sorted(counts.items()):
        bank = [r for r in rows if (r.capability, r.tenant) == (capability, tenant)]
        stable = sum(r.verdict == "stable" for r in bank)
        lines.append(f"{capability}  tenant {tenant}: {n} runs, {stable} of {len(bank)} steps stable")
        for r in bank:
            if r.verdict == "stable":
                continue
            found = ", ".join([f"first {r.first}", *(f"{k} {v}" for k, v in r.fallbacks.items()),
                               *(f"{k} {v}" for k, v in r.failures.items())])
            lines.append(f"  {r.verdict.upper():<17} {r.step}: {r.reason} [{found}]"
                         + (f", since {r.since}" if r.since else ""))
            if r.action:
                lines.append(f"  {'':<17} → {r.action}")
    return "\n".join(lines) if lines else "no replays found"


def as_json(rows: list[StepDrift]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=2)
