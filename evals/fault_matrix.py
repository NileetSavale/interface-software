"""Suite 1, fault matrix: every fault and data-driven outcome, on both flows, against the result the contract promises.

Expected labels are written down here before running, as the contract (docs/ARCHITECTURE.md §3): a missing member
is an answer (business_outcome), never a failure; an app error is fatal; an expired session is recovered on a
read-only flow and escalated on one that changes data; an unknown screen unattended stops rather than guessing.
"*" matches any code. No LLM.
"""
import fnmatch
from collections import Counter

from evals.common import HAPPY, artifact, label, mock_app, replay, save

TYPES = ["success", "business_outcome", "invalid_input", "needs_human", "failed"]


def cases() -> list[dict]:
    lookup, sub = HAPPY["lookup"], HAPPY["open_sub_account"]
    out = []
    for flow, happy in (("lookup", lookup), ("open_sub_account", sub)):
        out += [
            dict(flow=flow, case="happy path", inputs=happy, fault=None, expect="success"),
            dict(flow=flow, case="member doesn't exist", inputs={**happy, "memberId": "88888"}, fault=None,
                 expect="business_outcome/MEMBER_NOT_FOUND"),
            dict(flow=flow, case="restricted member", inputs={**happy, "memberId": "45678"}, fault=None,
                 expect="business_outcome/PERMISSION_DENIED"),
            dict(flow=flow, case="malformed member number", inputs={**happy, "memberId": "1234"}, fault=None,
                 expect="invalid_input"),
            dict(flow=flow, case="server error", inputs=happy, fault="server_error", expect="failed/APP_ERROR"),
            dict(flow=flow, case="session timeout", inputs=happy, fault="session_timeout",
                 # Signing on again and starting over is safe only when nothing has been changed
                 expect="success+recovered" if flow == "lookup" else "failed/SESSION_EXPIRED"),
            dict(flow=flow, case="slow page", inputs=happy, fault="slow", expect="success*"),
            dict(flow=flow, case="unknown notice page", inputs=happy, fault="interstitial", expect="failed/TARGET_UNRESOLVED"),
            dict(flow=flow, case="unknown dialog", inputs=happy, fault="unknown_dialog",
                 expect="failed/UNEXPECTED_DIALOG"),
            dict(flow=flow, case="app serves the wrong member", inputs=happy, fault="wrong_record",
                 expect="failed/WRONG_RECORD"),
        ]
    out += [
        dict(flow="lookup", case="number starting with 0", inputs={"memberId": "01234"}, fault=None,
             expect="business_outcome/INVALID_MEMBER_NUMBER"),
        dict(flow="open_sub_account", case="deposit below minimum", inputs={**sub, "initialDeposit": "5.00"},
             fault=None, expect="business_outcome/DEPOSIT_BELOW_MINIMUM"),
        dict(flow="open_sub_account", case="nickname breaks the input rule", inputs={**sub, "nickname": "Trip-fund!"},
             fault=None, expect="invalid_input"),
    ]
    return out


def kind(result_label: str) -> str:
    return result_label.split("/")[0].split("+")[0]


def run(n: int = 1, **_) -> dict:
    rows = []
    with mock_app() as app:
        for case in cases():
            for i in range(n):
                app.fault(case["fault"])
                result, seconds, _ = replay(app, artifact(case["flow"], app), case["inputs"])
                actual = label(result)
                rows.append({**{k: case[k] for k in ("flow", "case", "fault", "expect")}, "run": i + 1,
                             "actual": actual, "pass": fnmatch.fnmatchcase(actual, case["expect"]),
                             "at_step": getattr(result, "at_step", None), "seconds": round(seconds, 2),
                             "run_id": result.run_id})
                print(f"{'PASS' if rows[-1]['pass'] else 'FAIL'}  {case['flow']:<17} {case['case']:<32} {actual}",
                      flush=True)
        committed = app.confirmed()
    confusion = Counter((kind(r["expect"]), kind(r["actual"])) for r in rows)
    return {
        "n_per_case": n, "cases": len(cases()), "runs": len(rows), "passed": sum(r["pass"] for r in rows),
        # The commit point: nothing in this suite may ever open an account
        "sub_accounts_opened": committed,
        "confusion": {e: {a: confusion[(e, a)] for a in TYPES} for e in TYPES},
        "rows": rows,
    }


if __name__ == "__main__":
    save("fault_matrix", run())
