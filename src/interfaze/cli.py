"""interfaze: discover a capability with Claude, review and approve it, replay it without the model.

    interfaze mock-app
    interfaze discover goals/lookup_member_balance.json --input memberId=12345
    interfaze approve artifacts/core_banking.lookup_member_balance.json
    interfaze replay artifacts/core_banking.lookup_member_balance.json --input memberId=12345
    interfaze replay artifacts/core_banking.lookup_member_balance.json --tenant b --base-url http://localhost:4101 ...
    interfaze override artifacts/core_banking.lookup_member_balance.json --tenant b            (show the pending one)
    interfaze override artifacts/core_banking.lookup_member_balance.json --tenant b --approve
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from interfaze.artifact.io import dump_artifact, load_artifact

DEFAULT_BASE_URL = "http://localhost:4100"
# Exit codes a caller can branch on without parsing: a business outcome is an answer, not an error
EXIT = {"success": 0, "business_outcome": 0, "needs_human": 3, "invalid_input": 2, "failed": 1}


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    inputs = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--input takes name=value, got {pair!r}")
        inputs[name] = value
    return inputs


def show(result) -> None:
    print(result.model_dump_json(by_alias=True, exclude_none=True, indent=2), flush=True)


async def start_operator_page(port: int):
    from interfaze.operator import OperatorHub
    hub = OperatorHub()
    await hub.start(port)
    hub.on_publish = lambda i: print(f"\n>>> needs a person ({i.reason})\n>>> open {hub.url(i.id, port)}, take control,"
                                     f" work in the automation's browser window, then hand back\n", file=sys.stderr,
                                     flush=True)
    return hub


def pending_path(artifact_path: str, capability: str, tenant: str) -> Path:
    """Where drift on a tenant leaves its proposed override: beside the artifact, never inside it."""
    return Path(artifact_path).parent / capability / "overrides.pending" / f"{tenant}.json"


def cmd_replay(args) -> int:
    from interfaze.replay.executor import Replayer
    artifact = load_artifact(args.artifact)
    inputs = parse_inputs(args.input)
    tenant = {"tenant": args.tenant, "pending_dir": pending_path(args.artifact, artifact.capability.id, args.tenant).parent} \
        if args.tenant else {}
    if not args.attended:
        result = asyncio.run(Replayer(artifact, args.base_url, inputs, headed=args.headed,
                                      allow_draft=args.allow_draft, **tenant).run())
        show(result)
        return EXIT[result.status]

    async def attended():
        # What a calling agent sees: an answer by the deadline (maybe needs_human), then the late result
        from interfaze.runs import Runs
        hub = await start_operator_page(args.operator_port)
        runs = Runs(operator_hub=hub)
        result = await runs.invoke(artifact, args.base_url, inputs, deadline_s=args.deadline, headed=True,
                                   allow_draft=args.allow_draft, **tenant)
        if result.status in ("needs_human", "running"):
            show(result)
            print("... the run continues; waiting for its result", file=sys.stderr, flush=True)
            result = await runs.wait(result.run_id, 3600)
        await hub.stop()
        return result

    result = asyncio.run(attended())
    show(result)
    return EXIT.get(result.status, 1)


def cmd_approve(args) -> int:
    artifact = load_artifact(args.artifact)
    # Steps a person taught during discovery, or located by position, need a reviewer's look before anything runs
    # unattended: clear needsReview in the file once checked (often the fix is an outcome, not a step)
    if pending := [s.id for s in artifact.all_steps() if s.needs_review]:
        print(f"not approved: steps still flagged needsReview: {', '.join(pending)}", file=sys.stderr)
        return 1
    artifact = artifact.model_copy(update={"status": "approved"})
    Path(args.artifact).write_text(dump_artifact(artifact))
    # Approving is the person's sign-off that any wrong-record incidents were looked at: it closes the breaker
    from interfaze.evidence import Audit
    Audit().record("breaker_reset", f"{artifact.capability.id}@{artifact.capability.version}", by="approve")
    print(f"{artifact.capability.id}@{artifact.capability.version} approved for unattended replay")
    return 0


def cmd_override(args) -> int:
    """Show a tenant's pending override (proposed by drift), or approve it into the artifact. Approving checks it
    applies cleanly first; it's the reviewer's sign-off that the corrected names are the right controls."""
    from interfaze.artifact.overrides import OverrideError, apply
    from interfaze.artifact.schema import Override
    artifact = load_artifact(args.artifact)
    path = pending_path(args.artifact, artifact.capability.id, args.tenant)
    if not path.exists():
        print(f"no pending override for {args.tenant} ({path})", file=sys.stderr)
        return 1
    pending = Override.model_validate_json(path.read_text())
    for op in pending.ops:
        print(f"  {op.op} {op.step}" + (f" {op.path} = {json.dumps(op.value)}" if op.op == "set" else ""))
    if not args.approve:
        print(f"pending for {args.tenant}; approve with --approve")
        return 0
    current = artifact.overrides.get(args.tenant)
    merged = pending if current is None else current.model_copy(update={"ops": [
        *(op for op in current.ops if (op.step, getattr(op, "path", None)) not in
          {(p.step, getattr(p, "path", None)) for p in pending.ops}), *pending.ops]})
    updated = artifact.model_copy(update={"overrides": {**artifact.overrides, args.tenant: merged}})
    try:
        apply(updated, args.tenant)
    except (OverrideError, ValueError) as e:
        print(f"not approved: {e}", file=sys.stderr)
        return 1
    Path(args.artifact).write_text(dump_artifact(updated))
    path.unlink()
    print(f"override for {args.tenant} approved into {args.artifact}")
    return 0


def cmd_discover(args) -> int:
    from interfaze.discovery.loop import discover
    goal = json.loads(Path(args.goal).read_text())

    async def run():
        hub = await start_operator_page(args.operator_port) if args.attended else None
        outcome = await discover(goal, args.base_url, parse_inputs(args.input), model=args.model,
                                 max_steps=args.max_steps, budget_usd=args.budget,
                                 headed=args.headed or args.attended, out_dir=Path(args.out),
                                 escalation_model=args.escalation_model or None, operator_hub=hub)
        if hub:
            await hub.stop()
        return outcome

    outcome = asyncio.run(run())
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["status"] == "recorded" else 1


def cmd_mock_app(args) -> int:
    import uvicorn
    os.environ["VARIANT"] = args.variant
    # apps/ sits at the project root, which an installed entry point doesn't have on its import path
    uvicorn.run("apps.mock_core_banking.app:app", port=args.port, app_dir=str(Path(__file__).resolve().parents[2]))
    return 0


def load_dotenv(path: Path = Path(".env")):
    """KEY=value lines; the real environment wins over the file."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value:
            os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="interfaze")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("replay", help="run an artifact without the model")
    p.add_argument("artifact")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--allow-draft", action="store_true", help="development only: replay an unapproved artifact")
    p.add_argument("--attended", action="store_true", help="an operator is on hand: escalations pause for them "
                                                          "(headed browser, operator page on --operator-port)")
    p.add_argument("--deadline", type=float, default=30, help="attended: seconds before returning needs_human")
    p.add_argument("--operator-port", type=int, default=4200)
    p.add_argument("--tenant", help="run this tenant's version (its approved override); drift proposes one")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("override", help="review or approve a tenant's pending override")
    p.add_argument("artifact")
    p.add_argument("--tenant", required=True)
    p.add_argument("--approve", action="store_true")
    p.set_defaults(fn=cmd_override)

    p = sub.add_parser("approve", help="mark a reviewed artifact as approved for unattended replay")
    p.add_argument("artifact")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("discover", help="have Claude accomplish a goal and record it as an artifact")
    p.add_argument("goal")
    p.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--budget", type=float, default=1.0, help="USD cap for this run")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--out", default="artifacts")
    p.add_argument("--escalation-model", default="claude-opus-5-5", help="takes over when the first model is "
                                                                         "stuck; empty to skip that rung")
    p.add_argument("--attended", action="store_true", help="the last rung: hand the live browser to an operator")
    p.add_argument("--operator-port", type=int, default=4200)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("mock-app", help="serve the mock core-banking app")
    p.add_argument("--port", type=int, default=4100)
    p.add_argument("--variant", default="a", choices=["a", "b"], help="b: a second bank's templates")
    p.set_defaults(fn=cmd_mock_app)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
