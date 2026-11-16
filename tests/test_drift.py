"""The drift report's verdicts, from run logs written the way the replayer writes them."""
import json

from interface.drift import read_runs, render, report

CAP = {"capability": "core_banking.lookup_member_balance", "version": "1.0.0"}


def write_run(root, n, tenant, steps, failed=None, revision="r1"):
    """steps: step -> 0 (first strategy) or a fallback strategy name. failed: (step, errorClass)."""
    run_id = f"replay_20261116T10{n:04d}Z_aaaaaa"
    events = [{"at": f"2026-11-16T10:{n:02d}:00+00:00", "event": "replay_started", **CAP, "tenant": tenant,
               "revision": revision}]
    for step, how in steps.items():
        if how:
            events.append({"event": "drift", "step": step, "rank": 2, "strategy": how})
        events.append({"event": "acted", "step": step, "rank": 2 if how else 0, "strategy": how or "role"})
    result = {"runId": run_id}
    if failed:
        result |= {"atStep": failed[0], "errorClass": failed[1]}
    events.append({"event": "replay_finished", "status": "failed" if failed else "success", "result": result})
    (root / run_id).mkdir()
    (root / run_id / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return run_id


def verdicts(root, **kw):
    return {(r.tenant, r.step): r for r in report(read_runs(root), **kw)}


def test_a_bank_that_changed_is_an_override_to_approve(tmp_path):
    for n in range(3):
        write_run(tmp_path, n, "b", {"sign_on": 0, "search": 0})
    ids = [write_run(tmp_path, n, "b", {"sign_on": 0, "search": "anchor"}) for n in range(3, 6)]
    rows = verdicts(tmp_path)
    assert rows[("b", "sign_on")].verdict == "stable"
    search = rows[("b", "search")]
    assert search.verdict == "approve override"
    assert search.fallbacks == {"anchor": 3} and search.first == 3
    assert search.since == ids[0]
    assert "interface override" in search.action and "--tenant b" in search.action


def test_one_fallback_is_watched_not_acted_on(tmp_path):
    for n in range(5):
        write_run(tmp_path, n, "b", {"search": "anchor" if n == 2 else 0})
    assert verdicts(tmp_path)[("b", "search")].verdict == "watch"


def test_one_miss_is_watched_a_settled_miss_is_re_recorded(tmp_path):
    # Two members born the same day are AMBIGUOUS_TARGET by design: one miss says nothing about the bank
    write_run(tmp_path, 0, "b", {"pick": 0})
    write_run(tmp_path, 1, "b", {}, failed=("pick", "AMBIGUOUS_TARGET"))
    assert verdicts(tmp_path)[("b", "pick")].verdict == "watch"
    for n in range(2, 4):
        write_run(tmp_path, n, "b", {}, failed=("pick", "TARGET_UNRESOLVED"))
    row = verdicts(tmp_path)[("b", "pick")]
    assert row.verdict == "re-record" and "failed at this step (AMBIGUOUS_TARGET, TARGET_UNRESOLVED)" in row.reason


def test_a_fallback_that_keeps_changing_is_re_recorded(tmp_path):
    for n, how in enumerate(["anchor", "onlyInRegion", "anchor"]):
        write_run(tmp_path, n, "b", {"search": how})
    row = verdicts(tmp_path)[("b", "search")]
    assert row.verdict == "re-record" and "no strategy holds" in row.reason


def test_drift_with_no_tenant_has_no_override_to_approve(tmp_path):
    for n in range(3):
        write_run(tmp_path, n, None, {"search": "anchor"})
    row = verdicts(tmp_path)[("(default)", "search")]
    assert row.verdict == "re-record" and "no override" in row.reason


def test_approving_starts_a_new_revision(tmp_path):
    for n in range(4):
        write_run(tmp_path, n, "b", {"search": "anchor"}, revision="before")
    write_run(tmp_path, 4, "b", {"search": 0}, revision="after")  # the override approved: first strategy hits
    assert verdicts(tmp_path)[("b", "search")].verdict == "stable"


def test_banks_are_judged_apart(tmp_path):
    for n in range(3):
        write_run(tmp_path, 2 * n, "b", {"search": "anchor"})
        write_run(tmp_path, 2 * n + 1, "c", {"search": 0})
    rows = verdicts(tmp_path)
    assert rows[("b", "search")].verdict == "approve override" and rows[("c", "search")].verdict == "stable"
    text = render(list(rows.values()), read_runs(tmp_path))
    assert "APPROVE OVERRIDE" in text and "tenant c: 3 runs, 1 of 1 steps stable" in text


def test_window_keeps_only_recent_runs(tmp_path):
    for n in range(3):
        write_run(tmp_path, n, "b", {"search": "anchor"})
    for n in range(3, 6):
        write_run(tmp_path, n, "b", {"search": 0})
    assert verdicts(tmp_path, window=3)[("b", "search")].verdict == "stable"
    assert verdicts(tmp_path)[("b", "search")].verdict == "watch"


def test_a_cut_off_log_is_skipped(tmp_path):
    write_run(tmp_path, 0, "b", {"search": 0})
    (tmp_path / "replay_broken").mkdir()
    (tmp_path / "replay_broken" / "events.jsonl").write_text('{"event": "replay_started", "at"')
    assert len(read_runs(tmp_path)) == 1
