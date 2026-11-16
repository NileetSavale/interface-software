"""uv run python -m evals <suite|all|report> [--n N] [--budget USD] [--llm]

    fault-matrix   suite 1, no LLM          stability    suite 3, no LLM
    discovery      suite 2, Claude, $       safety       suite 4 (--llm adds the Claude cases, $)
    robustness     suite 5, no LLM          parabank     suite 6, no LLM, needs the network (a public demo bank)
    concurrency    suite 7, no LLM: replays at once across two banks, isolation and throughput
    terminal       suite 8, the green screen: faults on two banks, commit guard, stability (--llm adds discovery, $)
    report         rebuild evals/report.md from evals/results/
    all            every suite that needs neither an LLM nor the network, then the report

Each suite writes evals/results/<suite>.json (with date, commit and N); the report reads only those files.
"""
import argparse
import importlib
import sys

from evals.common import save
from interface.cli import load_dotenv

SUITES = {"fault-matrix": "fault_matrix", "stability": "stability", "robustness": "robustness",
          "safety": "safety", "discovery": "discovery", "parabank": "parabank", "concurrency": "concurrency",
          "terminal": "terminal"}
NO_LLM = ["fault-matrix", "stability", "robustness", "safety", "concurrency", "terminal"]


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("suite", choices=[*SUITES, "all", "report"])
    parser.add_argument("--n", type=int, help="runs per case/arm (suite default if omitted)")
    parser.add_argument("--budget", type=float, default=10.0, help="USD cap for the suites that call Claude")
    parser.add_argument("--llm", action="store_true", help="safety, terminal: also run the Claude discovery cases")
    args = parser.parse_args(argv)

    for suite in (NO_LLM if args.suite == "all" else [] if args.suite == "report" else [args.suite]):
        module = importlib.import_module(f"evals.{SUITES[suite]}")
        kw = {"budget": args.budget, "llm": args.llm, **({"n": args.n} if args.n else {})}
        print(f"── {suite} ──", flush=True)
        print(save(SUITES[suite], module.run(**kw)))
    from evals.report import write
    print(write())
    return 0


if __name__ == "__main__":
    sys.exit(main())
