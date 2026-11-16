"""Evidence on an app we didn't write: ParaBank, Parasoft's public demo bank (evidence/parabank/).

    uv run python scripts/parabank_evidence.py     # needs the network and ANTHROPIC_API_KEY; about $0.20

Registers a fresh fictional customer (scripts/parabank_setup.py), has Claude discover two read-only capabilities,
approves them, and replays each with no model: on the other account, and on inputs the app has no answer for. The
public demo resets its data now and then, which is why this is evidence and not an eval suite: the suites run on
the mock, where every run is repeatable.

Transfers and bill pay commit at once on ParaBank (no review screen), so every write path it has is a commitPath in
the goals: even a tampered artifact couldn't move money.
"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from parabank_setup import BASE, CUSTOMER, TRANSFER, register  # noqa: E402

from interface.artifact.io import dump_artifact, load_artifact  # noqa: E402
from interface.cli import load_dotenv  # noqa: E402
from interface.discovery.loop import discover  # noqa: E402
from interface.evidence import RUNS, Audit  # noqa: E402
from interface.replay.executor import Replayer  # noqa: E402

OUT = ROOT / "evidence" / "parabank"
GOALS = {"balance": ROOT / "goals" / "parabank_account_balance.json",
         "find": ROOT / "goals" / "parabank_find_transaction.json"}
cases: list[dict] = []


def keep(name: str, run_id: str, title: str, why: str, result: str, also: list[Path] = ()):
    dest = OUT / name
    shutil.copytree(RUNS / run_id, dest)
    for probe in sorted(RUNS.glob(f"{run_id}-probe*")):
        shutil.copytree(probe, dest / "probes" / probe.name.split("-")[-1])
    for path in also:
        shutil.copy(path, dest / path.name)
    cases.append({"dir": name, "title": title, "why": why, "result": result, "run_id": run_id})
    print(f"{name:<34} {result}", flush=True)


def label(result) -> str:
    code = getattr(result, "error_class", None) or getattr(result, "outcome_code", None)
    return f"{result.status}/{code}" if code else result.status


async def main():
    load_dotenv()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    accounts = await register()  # [original, opened from it]; TRANSFER moved from the second back to the first
    first, second = accounts[0], accounts[1]

    artifacts = {}
    for n, (key, goal_path) in enumerate(GOALS.items(), 1):
        goal = json.loads(goal_path.read_text())
        inputs = {"accountId": first, **({"amount": TRANSFER} if key == "find" else {})}
        outcome = await discover(goal, BASE, inputs, out_dir=ROOT / "artifacts")
        if outcome["status"] != "recorded":
            sys.exit(f"discovery of {key} ended {outcome['status']}: {outcome.get('reason')}")
        path = Path(outcome["artifact"])
        artifact = load_artifact(path).model_copy(update={"status": "approved"})  # reviewed: see the README
        path.write_text(dump_artifact(artifact))
        artifacts[key] = artifact
        probes = "; ".join(p["result"] for p in outcome.get("probes", []))
        keep(f"0{n}-discovery-{key}", outcome["run_id"], f"Real Claude discovery: {artifact.capability.title}",
             f"{outcome['steps']} steps, ${outcome['cost_usd']:.3f} including probes ({probes}).", "recorded",
             also=[path])

    async def replay(name, key, inputs, title, why):
        result = await Replayer(artifacts[key], BASE, inputs, audit=Audit(OUT / ".audit.jsonl")).run()
        keep(name, result.run_id, title, why, label(result))

    await replay("03-replay-balance-other-account", "balance", {"accountId": second},
                 "Replay, no model: another account", "Recorded on the first account; the link to the account is "
                 "{{inputs.accountId}} in the artifact, filled in with this run's account.")
    await replay("04-replay-balance-no-such-account", "balance", {"accountId": "99999"},
                 "An account that isn't there", "ParaBank lists no link for it and says nothing, so there's no "
                 "outcome to name: the run fails at the step that needs the link, not at sign-on (the probe found "
                 "no wording to learn, and said so).")
    await replay("05-replay-find-other-account", "find", {"accountId": second, "amount": TRANSFER},
                 "Replay, no model: the same search on another account", "Four identical \"Find Transactions\" "
                 "buttons, told apart by the line before each; the first result read by row: first (D27).")
    await replay("06-replay-find-no-transactions", "find", {"accountId": first, "amount": "9999.99"},
                 "An amount with no transactions", "The results table is empty: the read finds nothing and the run "
                 "fails, rather than returning a success with nothing in it. The probe's proposed outcome was rejected "
                 "because it matched the happy path too.")
    (OUT / ".audit.jsonl").unlink(missing_ok=True)

    rows = "\n".join(f"| [`{c['dir']}`]({c['dir']}/) | {c['title']} | `{c['result']}` | {c['why']} |" for c in cases)
    (OUT / "README.md").write_text(f"""# Evidence on a third-party app: ParaBank

Generated by `uv run python scripts/parabank_evidence.py` against [ParaBank]({BASE}/index.htm), Parasoft's public
demo bank: an app we didn't write, with a fictional customer registered for the run (no real bank, money or person).
Claude discovered each capability once; every replay ran with no model.

What this app tested that the mock didn't, and what changed because of it:

- **Captions above fields, not beside them** ("Username" in a paragraph over the box): the recorder refused to guess.
  Added the `after` anchor, the control after the text a reader meets before it.
- **Four identical "Find Transactions" buttons** with no labels or fieldsets: told apart by the line before each.
- **A link named by the account number**: recorded as `{{{{inputs.accountId}}}}`, never the number.
- **An onload focus move** that, about one run in three, sent part of the password into the username box: typed
  fields are now checked after typing and again before any click submits them.
- **Proof of sign-on that depended on the request**: now a heading the sign-on landed on.
- **A results list**: read by `row: first` (D27), only because the goal asks for the first result.
- **Writes that commit at once** (transfer, bill pay) through JSON calls: all declared `commitPaths`.

| Folder | What it shows | Result | |
|---|---|---|---|
{rows}

The artifacts are in `artifacts/parabank.*.json`. The data on a public demo changes and resets, so these runs are
evidence, not an eval: the suites in [`evals/`](../../evals/report.md) run on the mock.
""")
    forbidden = [CUSTOMER["firstName"], CUSTOMER["lastName"], CUSTOMER["ssn"], CUSTOMER["phoneNumber"],
                 CUSTOMER["address.street"], *accounts, __import__("os").environ["PARABANK_USER"],
                 __import__("os").environ["PARABANK_PASSWORD"]]
    leaks = [f"{p.relative_to(ROOT)}: {v}" for p in OUT.rglob("*") if p.is_file() and p.suffix in {".jsonl", ".json", ".txt", ".md"}
             for v in forbidden if v in p.read_text()]
    if leaks:
        sys.exit("LEAKS:\n" + "\n".join(leaks))
    print(f"{len(cases)} cases in {OUT}; scan clean")


if __name__ == "__main__":
    asyncio.run(main())
