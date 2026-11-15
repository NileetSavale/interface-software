"""Bakes the project's own records into docs/data.js for the GitHub Pages site (docs/index.html).

The page's facts come from the repo, never typed twice: decisions from docs/DECISIONS.md, eval numbers from
evals/results/*.json, the build timeline from git. Re-run after any of them change:

    uv run python scripts/build_site.py
"""
import html
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
FALLBACK_REPO = "https://github.com/nileetsavale/interfaze"

# Who moved each decision, read from the log's own wording; shown as filters on the page
KIND = {
    "D1": "overrode", "D2": "overrode", "D3": "my idea", "D4": "overrode", "D5": "overrode", "D6": "ai changed its mind",
    "D7": "agreed", "D8": "ai self-correction", "D9": "overrode", "D10": "overrode", "D11": "my challenge",
    "D12": "my challenge", "D13": "overrode", "D14": "overrode", "D15": "agreed", "D16": "ai self-correction",
    "D17": "ai self-correction", "D18": "agreed", "D19": "ai self-correction", "D20": "my choice",
    "D21": "ai proposal", "D22": "agreed",
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout.strip()


def inline(md: str) -> str:
    """The little markdown the decision log uses: **bold**, *italic*, `code`."""
    text = html.escape(md.strip(), quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)\*(?!\*)", r"<em>\1</em>", text)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", text)


def decisions() -> list[dict]:
    rows = []
    for line in (ROOT / "docs" / "DECISIONS.md").read_text().splitlines():
        if not re.match(r"\|\s*D\d+\s*\|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        id_, topic, proposed, chose, why = cells[:5]
        rows.append({"id": id_, "topic": inline(topic), "proposed": inline(proposed), "chose": inline(chose),
                     "why": inline(why), "kind": KIND.get(id_, "agreed")})
    return rows


def evals() -> dict:
    out = {}
    for path in sorted(RESULTS.glob("*.json")):
        data = json.loads(path.read_text())
        out[data["suite"]] = data
    return out


def summary(results: dict) -> dict:
    """The handful of numbers the hero and suite cards show, each traceable to a results file."""
    s = {}
    if fm := results.get("fault_matrix"):
        s["fault_matrix"] = {"passed": fm["passed"], "runs": fm["runs"], "opened": fm["sub_accounts_opened"],
                             "date": fm["date"], "commit": fm["commit"],
                             "misses": [f"bank {r.get('variant', 'a').upper()} {r['flow']}: {r['case']} → {r['actual']}"
                                        for r in fm["rows"] if not r["pass"]]}
    if st := results.get("stability"):
        s["stability"] = {"ok": sum(round(a["success_rate"] * a["n"]) for a in st["arms"]),
                          "runs": sum(a["n"] for a in st["arms"]),
                          "distinct": max(a["distinct_outputs"] for a in st["arms"]),
                          "p50": min(a["p50_s"] for a in st["arms"]), "date": st["date"], "commit": st["commit"]}
    if rb := results.get("robustness"):
        s["robustness"] = {"ok": sum(f["results"].get("success", 0) for f in rb["flows"]),
                           "runs": sum(f["seeds"] for f in rb["flows"]),
                           "wrong": sum(f["silent_wrong"] for f in rb["flows"]), "date": rb["date"], "commit": rb["commit"]}
    if sf := results.get("safety"):
        llm = sf.get("discovery", {}).get("rows", [])
        s["safety"] = {"passed": sf["passed"], "total": sf["total"], "gaps": sf["known_gaps"],
                       "llm_passed": sum(r["pass"] for r in llm), "llm_total": len(llm),
                       "llm_spent": sf.get("discovery", {}).get("spent_usd"),
                       "harm": sum(r["accounts_opened"] + r["credentials_posted"] for r in llm),
                       "date": sf["date"], "commit": sf["commit"]}
    if dc := results.get("discovery"):
        arms = dc["arms"]
        s["discovery"] = {"recorded": sum(a["recorded"] for a in arms), "runs": sum(a["n"] for a in arms),
                          "replays": sum(a["replays_on_other_member"] for a in arms),
                          "median_cost": [a["median_cost_usd"] for a in arms], "spent": dc["spent_usd"],
                          "arms": [{k: a[k] for k in ("flow", "n", "recorded", "replays_on_other_member", "median_turns",
                                                      "median_cost_usd", "escalated_runs")} for a in arms],
                          "date": dc["date"], "commit": dc["commit"]}
    return s


def commits() -> list[dict]:
    log = git("log", "--reverse", "--format=%h%x09%ad%x09%s%x09%b%x1e", "--date=short")
    out = []
    for entry in filter(None, (e.strip() for e in log.split("\x1e"))):
        sha, date, subject, body = (entry.split("\t", 3) + [""])[:4]
        body = "\n".join(l for l in body.splitlines() if not l.startswith("Co-Authored-By"))
        out.append({"sha": sha, "date": date, "subject": subject, "body": body.strip()})
    return out


def repo_url() -> str:
    remote = git("remote", "get-url", "origin")
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.split(":", 1)[1]
    return remote.removesuffix(".git") or FALLBACK_REPO


def main():
    results = evals()
    data = {"repo": repo_url(), "decisions": decisions(), "evals": summary(results), "commits": commits(),
            "tests": len(re.findall(r"^def test_|^async def test_", "".join(
                p.read_text() for p in (ROOT / "tests").glob("test_*.py")), re.M))}
    out = ROOT / "docs" / "data.js"
    out.write_text("// Generated by scripts/build_site.py from the repo's own records. Don't edit by hand.\n"
                   f"window.SITE = {json.dumps(data, indent=1)};\n")
    (ROOT / "docs" / ".nojekyll").touch()
    print(f"{out}: {len(data['decisions'])} decisions, {len(data['commits'])} commits, suites {sorted(data['evals'])}")


if __name__ == "__main__":
    main()
