from pydantic import TypeAdapter, ValidationError
import pytest

from interface.replay.result import BusinessOutcome, Failed, ReplayResult, Success

parse = TypeAdapter(ReplayResult).validate_python
BASE = {"runId": "run_1", "completedAt": "2026-11-14T00:00:00Z"}


def test_status_picks_the_shape():
    assert isinstance(parse({**BASE, "status": "success", "outputs": {"savingsBalance": "1234.56"}}), Success)
    assert isinstance(parse({**BASE, "status": "business_outcome", "code": "MEMBER_NOT_FOUND", "atStep": "submit_search"}),
                      BusinessOutcome)


def test_failure_must_say_where_what_and_why():
    with pytest.raises(ValidationError):
        parse({**BASE, "status": "failed", "atStep": "read_balance", "errorClass": "TIMEOUT"})
    failed = parse({**BASE, "status": "failed", "atStep": "read_balance", "errorClass": "WRONG_RECORD",
                    "expected": "member # = {{inputs.memberId}}", "observed": "member # ****0451"})
    assert isinstance(failed, Failed)


def test_unknown_failure_class_is_rejected():
    with pytest.raises(ValidationError):
        parse({**BASE, "status": "failed", "atStep": "x", "errorClass": "OOPS", "expected": "a", "observed": "b"})


def test_recoveries_ride_along_with_success():
    result = parse({**BASE, "status": "success", "outputs": {},
                    "recovered": [{"code": "SESSION_EXPIRED", "stepId": "submit_search", "attempt": 1,
                                   "action": "ran subflow login"}]})
    assert result.recovered[0].code == "SESSION_EXPIRED"


def test_invalid_input_needs_at_least_one_error():
    with pytest.raises(ValidationError):
        parse({**BASE, "status": "invalid_input", "errors": []})
