"""Suite 1, fault matrix: every fault and data-driven outcome, on both flows, against the result the contract promises.

Expected labels are written down here before running, as the contract (docs/ARCHITECTURE.md §3): a missing member
is an answer (business_outcome), never a failure; an app error is fatal; an expired session is recovered on a
read-only flow and escalated on one that changes data; an unknown screen unattended stops rather than guessing.
"*" matches any code. No LLM.

Both banks: the artifacts were recorded on bank A. On bank B (relabelled search button, unlabelled Member # field)
each flow first runs once with no override: it must succeed through fallbacks, report drift and propose an override
for B. That proposal is then treated as approved, and every case runs again on B: the same contract must hold, and
the happy path must run without drift.

Bank C (B, plus the menu's "Member Inquiry" renamed "Member Lookup") has no override at all. Without a page map the
happy path stops at once. With C's map, learned from one verified lookup run on C, every case runs again: replay
gets past what no recorded strategy survives through the map's bounded fallback, including for the sub-account
flow, which the map never saw. Each run gets a frozen copy of the map, so one run's learning can't steer the next.
"""
import fnmatch
import tempfile
from collections import Counter
from pathlib import Path

from evals.common import HAPPY, artifact, bank_c_map, label, mock_app, replay, save
from interface.artifact.schema import Override

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
            # Found on ParaBank: values filled in by a background call after load. Empty isn't someone else
            dict(flow=flow, case="values filled in after the page loads", inputs=happy, fault="late_fill",
                 expect="success*"),
            dict(flow=flow, case="the wrong member, filled in after load", inputs=happy, fault="late_wrong_record",
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


def list_cases() -> list[dict]:
    """Bank A only: a row chosen from several by the input it shows, never by position, and a row pages away (D28)."""
    find, hist = HAPPY["find_member"], HAPPY["history"]
    return [
        dict(flow="find_member", case="one of several by date of birth", inputs=find, fault=None, expect="success"),
        dict(flow="find_member", case="three Chens, one born that day", inputs={"lastName": "Chen", "dob": "07/04/1985"},
             fault=None, expect="success"),
        dict(flow="find_member", case="two Chens born the same day", inputs={"lastName": "Chen", "dob": "02/14/1990"},
             fault=None, expect="failed/AMBIGUOUS_TARGET"),
        dict(flow="find_member", case="no Chen born that day", inputs={"lastName": "Chen", "dob": "01/01/2000"},
             fault=None, expect="failed/TARGET_UNRESOLVED"),
        dict(flow="find_member", case="no member has that surname", inputs={**find, "lastName": "Zzyzx"}, fault=None,
             expect="business_outcome/MEMBER_NOT_FOUND"),
        dict(flow="find_member", case="the right row, the wrong member served", inputs=find, fault="wrong_record",
             expect="failed/WRONG_RECORD"),
        dict(flow="history", case="on page 1", inputs={**hist, "reference": "REF-0003"}, fault=None, expect="success"),
        dict(flow="history", case="on page 3", inputs=hist, fault=None, expect="success"),
        dict(flow="history", case="last row of the last page", inputs={**hist, "reference": "REF-0035"}, fault=None,
             expect="success"),
        dict(flow="history", case="on no page", inputs={**hist, "reference": "REF-0099"}, fault=None,
             expect="failed/TARGET_UNRESOLVED"),
        dict(flow="history", case="past the page limit", inputs=hist, fault=None, expect="failed/TARGET_UNRESOLVED",
             max_pages=1),
        dict(flow="history", case="member doesn't exist", inputs={**hist, "memberId": "99999"}, fault=None,
             expect="business_outcome/MEMBER_NOT_FOUND"),
    ]


def kind(result_label: str) -> str:
    return result_label.split("/")[0].split("+")[0].rstrip("*")


def run(n: int = 1, **_) -> dict:
    rows, proposals = [], {}

    def record(variant: str, case: dict, result, seconds: float, i: int = 0):
        actual = label(result)
        rows.append({"variant": variant, **{k: case[k] for k in ("flow", "case", "fault", "expect")}, "run": i + 1,
                     "actual": actual, "pass": fnmatch.fnmatchcase(actual, case["expect"]),
                     "at_step": getattr(result, "at_step", None), "seconds": round(seconds, 2),
                     "run_id": result.run_id})
        print(f"{'PASS' if rows[-1]['pass'] else 'FAIL'}  {variant} {case['flow']:<17} {case['case']:<32} {actual}",
              flush=True)

    with mock_app() as app:
        for variant in ("a", "b", "c"):
            app.variant(variant)
            tenants, bank_map = {}, None
            if variant == "c":
                bank_map, script, discover_with = bank_c_map()
                learned, _ = discover_with(app.base_url, script, bank_map, Path(tempfile.mkdtemp()))
                assert learned["status"] == "recorded", learned
                for flow in ("lookup", "open_sub_account"):
                    case = dict(flow=flow, case="no page map", fault=None, expect="failed/*")
                    app.fault(None)
                    result, seconds, _ = replay(app, artifact(flow, app), HAPPY[flow], tenant="c",
                                                pending_dir=Path(tempfile.mkdtemp()))
                    record(variant, case, result, seconds)
            if variant == "b":
                # First contact with bank B: drift, and an override proposed from what the fallbacks found
                for flow in ("lookup", "open_sub_account"):
                    pending = Path(tempfile.mkdtemp())
                    case = dict(flow=flow, case="first run, no override yet", fault=None, expect="success+drift")
                    app.fault(None)
                    result, seconds, _ = replay(app, artifact(flow, app), HAPPY[flow], tenant="b", pending_dir=pending)
                    record(variant, case, result, seconds)
                    if (pending / "b.json").exists():
                        proposal = Override.model_validate_json((pending / "b.json").read_text())
                        proposals[flow] = proposal.model_dump(by_alias=True, exclude_none=True)
                        tenants[flow] = proposal
            for case in cases():
                if variant == "c" and case["expect"].startswith("success"):
                    case = {**case, "expect": "success*"}  # the map's fallback is a recovery, reported beside success
                subject = artifact(case["flow"], app)
                if case["flow"] in tenants:  # the proposal, approved
                    subject = subject.model_copy(update={"overrides": {"b": tenants[case["flow"]]}})
                for i in range(n):
                    app.fault(case["fault"])
                    result, seconds, _ = replay(app, subject, case["inputs"], tenant=variant,
                                                pending_dir=Path(tempfile.mkdtemp()),
                                                page_map=bank_map.frozen() if bank_map else None)
                    record(variant, case, result, seconds, i)
        app.variant("a")
        for case in list_cases():
            subject = artifact(case["flow"], app)
            if case.get("max_pages"):
                subject = subject.model_copy(update={"steps": [
                    s.model_copy(update={"max_pages": case["max_pages"]}) if s.action == "pageUntil" else s
                    for s in subject.steps]})
            app.fault(case["fault"])
            result, seconds, _ = replay(app, subject, case["inputs"], pending_dir=Path(tempfile.mkdtemp()))
            record("a", case, result, seconds)
        committed = app.confirmed()
    confusion = Counter((kind(r["expect"]), kind(r["actual"])) for r in rows)
    return {
        "n_per_case": n, "cases": len(cases()) + len(list_cases()), "variants": ["a", "b", "c"], "runs": len(rows),
        "passed": sum(r["pass"] for r in rows), "proposed_overrides": proposals,
        # The commit point: nothing in this suite may ever open an account
        "sub_accounts_opened": committed,
        "confusion": {e: {a: confusion[(e, a)] for a in TYPES} for e in TYPES},
        "rows": rows,
    }


if __name__ == "__main__":
    save("fault_matrix", run())
