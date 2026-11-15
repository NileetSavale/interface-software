"""interfaze: discover a capability with Claude, review and approve it, replay it without the model.

    interfaze mock-app
    interfaze discover goals/lookup_member_balance.json --input memberId=12345
    interfaze approve artifacts/core_banking.lookup_member_balance.json
    interfaze replay artifacts/core_banking.lookup_member_balance.json --input memberId=12345
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


def cmd_replay(args) -> int:
    from interfaze.replay.executor import Replayer
    artifact = load_artifact(args.artifact)
    result = asyncio.run(Replayer(artifact, args.base_url, parse_inputs(args.input), headed=args.headed,
                                  allow_draft=args.allow_draft).run())
    print(result.model_dump_json(by_alias=True, exclude_none=True, indent=2))
    return EXIT[result.status]


def cmd_approve(args) -> int:
    artifact = load_artifact(args.artifact)
    artifact = artifact.model_copy(update={"status": "approved"})
    Path(args.artifact).write_text(dump_artifact(artifact))
    # Approving is the person's sign-off that any wrong-record incidents were looked at: it closes the breaker
    from interfaze.evidence import Audit
    Audit().record("breaker_reset", f"{artifact.capability.id}@{artifact.capability.version}", by="approve")
    print(f"{artifact.capability.id}@{artifact.capability.version} approved for unattended replay")
    return 0


def cmd_discover(args) -> int:
    from interfaze.discovery.loop import discover
    goal = json.loads(Path(args.goal).read_text())
    outcome = asyncio.run(discover(goal, args.base_url, parse_inputs(args.input), model=args.model,
                                   max_steps=args.max_steps, budget_usd=args.budget, headed=args.headed,
                                   out_dir=Path(args.out)))
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["status"] == "recorded" else 1


def cmd_mock_app(args) -> int:
    import uvicorn
    uvicorn.run("apps.mock_core_banking.app:app", port=args.port)
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
    p.set_defaults(fn=cmd_replay)

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
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("mock-app", help="serve the mock core-banking app")
    p.add_argument("--port", type=int, default=4100)
    p.set_defaults(fn=cmd_mock_app)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
