"""The standard outcome library (docs/ARCHITECTURE.md §3, "where outcomes come from", source 2).

States almost every legacy back office has, so no discovery run has to meet them to be ready for them. They're
added to every recorded artifact as drafts; the reviewer keeps, edits or deletes them before approving. Business
outcomes specific to one flow (no such member) come from discovery and review, not from here.
"""
from interfaze.artifact.schema import Outcome, Recovery, Regex, RunSubflow, Visible, WaitLonger

SIGN_ON = "sign_on"


def _text(pattern: str) -> Visible:
    return Visible(text=Regex(matches=pattern))


def standard_outcomes(*, has_sign_on: bool, read_only: bool) -> list[Outcome]:
    outcomes = [
        Outcome(code="APP_ERROR", kind="fatal",
                detect=_text(r"(?i)internal server error|an unexpected error|service unavailable"),
                description="The application failed. Stop and keep the evidence."),
        Outcome(code="PERMISSION_DENIED", kind="business",
                detect=_text(r"(?i)not authori[sz]ed|access denied|insufficient (privileges|permissions)"),
                description="The operator account may not do this. An answer for the caller, not a fault."),
        Outcome(code="UNEXPECTED_DIALOG", kind="escalate", detect=Visible(role="dialog"),
                description="A dialog the capability doesn't know. A person decides."),
        Outcome(code="SLOW_LOAD", kind="recoverable", detect=_text(r"(?i)^\s*(loading|please wait)\b"),
                recovery=Recovery(do=WaitLonger(timeout_ms=5_000), max_attempts=2),
                description="The app shows a wait message. Wait longer, then carry on."),
    ]
    if has_sign_on:
        # Signing on again lands on the home screen. A read-only capability can simply run its steps again; one that
        # changed something can't, so for it an expired session is a person's call.
        expired = _text(r"(?i)session (has )?(expired|timed out)")
        outcomes.append(
            Outcome(code="SESSION_EXPIRED", kind="recoverable", detect=expired,
                    recovery=Recovery(do=RunSubflow(subflow=SIGN_ON), max_attempts=1, resume="start"),
                    description="Sign on again and run the steps from the first.")
            if read_only else
            Outcome(code="SESSION_EXPIRED", kind="escalate", detect=expired,
                    description="The session ended part-way through a change. A person checks what was saved.")
        )
    return outcomes
