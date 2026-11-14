"""What a caller gets back from invoking a capability (docs/ARCHITECTURE.md §4).

Five terminal shapes, told apart by `status`. The split that matters most: a business outcome ("no such member") is
a legitimate answer, not a failure, and a failure always says where it happened, what was expected and what was seen.
Recoveries aren't a status: they happen inside a run and are reported beside its result.
"""
from typing import Annotated, Literal

from pydantic import Field

from interfaze.artifact.schema import Model

FailureClass = Literal[
    "TARGET_UNRESOLVED",    # no ranked strategy found the control (unattended; attended goes to needs_human)
    "AMBIGUOUS_TARGET",     # every strategy matched more than one control; we never guess
    "EXPECT_FAILED",        # a step's postcondition didn't hold
    "CHECKPOINT_FAILED",    # the success checkpoint didn't hold
    "WRONG_RECORD",         # the record on screen isn't the one asked for (D16)
    "FATAL_OUTCOME",        # a declared fatal outcome matched, e.g. APP_ERROR; see outcomeCode
    "POLICY_VIOLATION",     # the step would leave the allowlist or exceed the policy's risk
    "RECOVERY_EXHAUSTED",   # a recoverable outcome kept recurring past its attempts, or the run's cap of 3
    "TIMEOUT",              # a wait ran out with no branch matched
    "NOT_APPROVED",         # a draft artifact was invoked unattended
    "SURFACE_ERROR",        # the browser or app surface itself failed
]


class RecoveryEvent(Model):
    code: str = Field(description="The recoverable outcome, or MAP_FALLBACK for a map-assisted step (§7a).")
    step_id: str
    attempt: int
    action: str = Field(description="What was done, e.g. 'ran subflow login'.")
    map_version: str | None = None


class DriftEvent(Model):
    """The control was found, but not by the strategy recorded first. The run still succeeds (§4 drift)."""
    step_id: str
    hit_rank: int = Field(ge=1, description="Index of the strategy that matched; 0 would be no drift.")
    strategy: str
    proposed_override: str | None = Field(default=None, description="Path of the draft override written for review.")


class EvidenceRef(Model):
    kind: Literal["screenshot", "a11y_snapshot", "trace", "log"]
    path: str


class _Result(Model):
    run_id: str
    completed_at: str = Field(description="So a caller collecting a late result can judge whether it's stale.")


class _Ran(_Result):
    recovered: list[RecoveryEvent] = Field(default_factory=list)
    drift: list[DriftEvent] = Field(default_factory=list)


class Success(_Ran):
    status: Literal["success"] = "success"
    outputs: dict[str, object]


class BusinessOutcome(_Ran):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    at_step: str
    details: dict[str, object] = Field(default_factory=dict)


class InputError(Model):
    input: str
    rule: str
    message: str


class InvalidInput(_Result):
    """Rejected before any UI was touched, so it isn't a business outcome: the app was never asked."""
    status: Literal["invalid_input"] = "invalid_input"
    errors: list[InputError] = Field(min_length=1)


class NeedsHuman(_Result):
    """The run is paused for an operator and continues in the background; collect it later by run_id."""
    status: Literal["needs_human"] = "needs_human"
    intervention_id: str
    at_step: str
    reason: str


class Failed(_Ran):
    status: Literal["failed"] = "failed"
    at_step: str
    error_class: FailureClass
    expected: str
    observed: str
    outcome_code: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


ReplayResult = Annotated[
    Success | BusinessOutcome | InvalidInput | NeedsHuman | Failed, Field(discriminator="status"),
]


class RunPending(Model):
    """What getResult/wait return while a run that outlived its caller's deadline is still going."""
    run_id: str
    status: Literal["running", "paused"]
    since: str
