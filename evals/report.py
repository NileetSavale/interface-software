"""evals/report.md, rebuilt from evals/results/*.json alone: every number in it comes from a saved run."""
import json

from evals.common import RESULTS, ROOT

TYPES = ["success", "business_outcome", "invalid_input", "needs_human", "failed"]


def _load(suite: str) -> dict | None:
    path = RESULTS / f"{suite}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _stamp(data: dict) -> str:
    return f"_Run {data['date'][:16].replace('T', ' ')} UTC at commit `{data['commit']}`._"


def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def fault_matrix(d: dict) -> str:
    cases = _table(["bank", "flow", "case", "fault", "expected", "actual", ""],
                   [[r["variant"].upper(), r["flow"], r["case"], r["fault"] or "–", f"`{r['expect']}`", f"`{r['actual']}`",
                     "✅" if r["pass"] else "❌"] for r in d["rows"]])
    confusion = _table(["expected ↓ / actual →", *TYPES],
                       [[e, *(d["confusion"][e][a] or "·" for a in TYPES)] for e in TYPES if any(d["confusion"][e].values())])
    return f"""## 1. Fault matrix (no LLM)
{_stamp(d)} {d['cases']} cases × N={d['n_per_case']} = {d['runs']} replays of the two discovered artifacts.

**{d['passed']}/{d['runs']} match the contract.** Sub-accounts opened across the whole suite: **{d['sub_accounts_opened']}**.

{confusion}

{cases}
"""


def stability(d: dict) -> str:
    rows = [[a["flow"], a["condition"], a["n"], f"{a['success_rate']:.0%}", a["distinct_outputs"], a["p50_s"], a["p95_s"],
             ", ".join(f"{k} ×{v}" for k, v in a["unsteady_steps"].items()) or "none"] for a in d["arms"]]
    return f"""## 3. Replay stability (no LLM)
{_stamp(d)} N={d['n_per_arm']} replays per arm, same inputs every time.

{_table(["flow", "condition", "N", "success", "distinct outputs", "p50 s", "p95 s", "unsteady steps"], rows)}

"Distinct outputs" must be 1: every run reads the same values. "Unsteady steps" are steps that needed a fallback
strategy or a recovery on some runs. Durations include launching a fresh browser for each run.
"""


def robustness(d: dict) -> str:
    parts = [f"""## 5. Recorder robustness (no LLM)
{_stamp(d)} {d['seeds']} shuffled layouts per flow (frames, fieldsets, rows, columns and nesting reordered; labels kept).
"""]
    for f in d["flows"]:
        rows = [[s["step"], s["same"], s["WRONG"] or "·", s["unresolved"] or "·", s["not run"] or "·",
                 ", ".join(f"{k} ×{v}" for k, v in s["ranks"].items())] for s in f["steps"]]
        parts.append(f"""**{f['flow']}**: {f['success_rate']:.0%} of runs succeed; runs that succeeded on the wrong control or with
wrong values: **{f['silent_wrong']}**.

{_table(["step", "same control", "WRONG control", "unresolved", "not run", "rank:strategy"], rows)}
""")
    return "\n".join(parts)


def safety(d: dict) -> str:
    rows = [[r["case"], r["guard"], r["observed"], "✅" if r["pass"] else ("⚠️ known gap" if r["known_gap"] else "❌")]
            for r in d["rows"]]
    out = f"""## 4. Safety and adversarial
{_stamp(d)} Deterministic cases (no LLM): **{d['passed']}/{d['total']} pass**, {d['known_gaps']} known gap(s).

{_table(["case", "guard", "observed", ""], rows)}
"""
    if llm := d.get("discovery"):
        rows = [[r["case"], r["run"], r["status"], r["attempted"], r["refused_by_policy"], r["accounts_opened"],
                 r["credentials_posted"], ", ".join(r["bait_steps_in_artifact"]) or "none",
                 r.get("steps_needing_review", "–"), "✅" if r["pass"] else "❌"]
                for r in llm["rows"]]
        out += f"""
**Discovery with Claude in front of the hostile pages**: N={llm['n_per_case']} per case, ${llm['spent_usd']:.2f} spent
of a ${llm['budget_usd']:.2f} cap. "Attempted" counts tool calls that went for the bait; "refused" counts those the
policy stopped.

{_table(["case", "run", "status", "attempted", "refused", "accounts opened", "credentials posted", "bait steps recorded", "steps flagged for review", ""], rows)}
"""
    return out


def discovery(d: dict) -> str:
    rows = [[a["flow"], a["variant"], a["map"], a["n"], f"{a['recorded']}/{a['n']}", f"{a['replays_on_other_member']}/{a['n']}",
             a["median_turns"], a.get("median_known_actions", "–"), f"${a['median_cost_usd']:.3f}", a["nudged_runs"],
             a["escalated_runs"]] for a in d["arms"]]
    return f"""## 2. Discovery reliability (Claude)
{_stamp(d)} ${d['spent_usd']:.2f} spent of a ${d['budget_usd']:.2f} cap. Map on: seeded from one approved lookup replay on
this bank (map version `{d.get('map_seed', '–')}`), a frozen copy per run.

{_table(["flow", "variant", "map", "N", "recorded", "replays on another member", "median turns", "known actions used", "median cost", "nudged", "escalated"], rows)}

Recorded on member 12345, replayed on 34567. Cost includes the probes. With N this small, a difference of one run
between arms is noise.
"""


def write() -> str:
    sections = [(s, fn) for s, fn in (("fault_matrix", fault_matrix), ("discovery", discovery), ("stability", stability),
                                      ("safety", safety), ("robustness", robustness))]
    body = [fn(data) for s, fn in sections if (data := _load(s))]
    missing = [s for s, _ in sections if not _load(s)]
    text = f"""# Evaluation report

Generated by `uv run python -m evals report` from `evals/results/*.json`; each section says when and at which commit
it ran, and N. Suites and what they measure: `docs/ARCHITECTURE.md` §9a.
{f"{chr(10)}Not run yet: {', '.join(missing)}.{chr(10)}" if missing else ""}
""" + "\n".join(body)
    path = ROOT / "evals" / "report.md"
    path.write_text(text)
    return str(path)
