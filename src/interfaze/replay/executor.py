"""Deterministic replay: an artifact plus inputs, run with no model in the loop (docs/ARCHITECTURE.md §4).

Per step: check outcomes → resolve the target → policy → act → wait → check outcomes → check the postcondition.
Same inputs, same steps: the only branching is on outcomes the artifact declares. Recoveries happen inside the run
and are reported beside the result; drift (a fallback strategy hit) never fails a run, it's reported.
"""
import asyncio
import json
import re
from collections import Counter
from pathlib import Path

from playwright.async_api import async_playwright

from interfaze.artifact import overrides
from interfaze.artifact.schema import (
    Artifact, ClickThrough, IdentityMatches, InputSpec, OutcomeSeen, Outcome, OutputPresent, RunSubflow, Step,
    TEMPLATE, Visible, WaitLonger,
)
from interfaze.artifact.templates import TemplateError, render
from interfaze.evidence import Audit, RunLog, new_run_id, now, structure_only
from interfaze.operator import _public
from interfaze.session import ControlLost, Intervention, Session
from interfaze.policy import PolicyViolation, check_action, check_url
from interfaze.replay.result import (
    BusinessOutcome, DriftEvent, EvidenceRef, Failed, FailureClass, InputError, InvalidInput, RecoveryEvent, Success,
)
from interfaze.surface.base import Resolved, SurfaceError
from interfaze.surface.web import WebSurface

DEFAULT_TIMEOUT_MS = 10_000
# A control's accessible name, for proposing an override. Never an input's value: that's what the user typed.
LIVE_NAME_JS = """el => {
  if (el.matches('input[type=submit],input[type=button],input[type=reset]')) return el.value || '';
  if (el.matches('input,select,textarea')) return el.getAttribute('aria-label') || (el.labels && el.labels[0]
    ? el.labels[0].innerText : '');
  return el.getAttribute('aria-label') || el.innerText || '';
}"""
MAX_RECOVERIES_PER_RUN = 3


class _Stop(Exception):
    """Ends the run with a result. Raised from deep inside a step so each check doesn't thread a return value."""
    def __init__(self, result):
        self.result = result


class _Retry(Exception):
    """A recovery ran. If the step had already acted it's done and the run moves on; otherwise it runs again."""
    def __init__(self, step_done: bool):
        self.step_done = step_done


class _Restart(Exception):
    """A recovery ran and left the app somewhere else; run the steps again from the first."""


def validate_inputs(artifact: Artifact, inputs: dict[str, str]) -> list[InputError]:
    errors = [InputError(input=name, rule="required", message="no value supplied")
              for name in artifact.inputs if name not in inputs]
    errors += [InputError(input=name, rule="declared", message="not an input of this capability")
               for name in inputs if name not in artifact.inputs]
    for name, value in inputs.items():
        spec = artifact.inputs.get(name)
        if spec and (problem := _check_value(spec, str(value))):
            errors.append(InputError(input=name, rule=problem[0], message=problem[1]))
    return errors


def _check_value(spec: InputSpec, value: str) -> tuple[str, str] | None:
    if spec.pattern and not re.fullmatch(spec.pattern, value):
        return "pattern", f"doesn't match {spec.pattern}"
    if spec.type == "integer" and not re.fullmatch(r"-?\d+", value):
        return "type", "not an integer"
    if spec.type == "enum" and value not in (spec.values or []):
        return "enum", f"not one of {spec.values}"
    return None


def parse_value(text: str, kind: str) -> str | int:
    if kind == "money":
        negative = text.startswith("(") or "-" in text
        digits = re.sub(r"[^\d.]", "", text)
        return f"-{digits}" if negative and digits else digits
    if kind == "integer":
        return int(re.sub(r"[^\d-]", "", text))
    return text


def last4(value: str) -> str:
    return f"****{value[-4:]}" if len(value) > 4 else "****"


class Replayer:
    def __init__(self, artifact: Artifact, base_url: str, inputs: dict[str, str], *, headed: bool = False,
                 allow_draft: bool = False, env: dict[str, str] | None = None, run_id: str | None = None,
                 audit: Audit | None = None, operator_hub=None, pause_timeout_s: float = 600,
                 tenant: str | None = None, pending_dir: Path | None = None):
        # The tenant's own version: the base with that tenant's approved override applied (§7). An override that
        # doesn't apply to this base version is refused at load (OverrideError), never skipped silently
        self.tenant = tenant
        self.base = artifact
        artifact = overrides.apply(artifact, tenant) if tenant else artifact
        self.pending_dir = pending_dir or Path("artifacts") / artifact.capability.id / "overrides.pending"
        self.artifact = artifact
        self.base_url = base_url.rstrip("/")
        self.inputs = {k: str(v) for k, v in inputs.items()}
        self.headed = headed
        self.allow_draft = allow_draft
        self.env = env
        self.run_id = run_id or new_run_id("replay")
        self.audit = audit or Audit()
        # Attended: an operator channel is open, so escalations pause for a person instead of failing
        self.hub = operator_hub
        self.pause_timeout_s = pause_timeout_s
        self.key = f"{artifact.capability.id}@{artifact.capability.version}"
        self.outputs: dict[str, object] = {}
        self.recovered: list[RecoveryEvent] = []
        self.drift: list[DriftEvent] = []
        self._recoveries: Counter[str] = Counter()
        self.surface: WebSurface | None = None
        self.log = RunLog(self.run_id)
        self.session = Session(self.log)
        self._next: Step | None = None
        # Sensitive inputs and every secret the artifact uses are masked wherever they'd be written
        for name, spec in artifact.inputs.items():
            if spec.sensitive and name in self.inputs:
                self.log.mask(self.inputs[name], f"{{{{inputs.{name}}}}}")

    def _fail(self, step: str, error_class: FailureClass, expected: str, observed: str, **extra) -> _Stop:
        return _Stop(Failed(run_id=self.run_id, completed_at=now(), at_step=step, error_class=error_class,
                            expected=expected, observed=self.log.redact(observed), recovered=self.recovered,
                            drift=self.drift, **extra))

    async def run(self):
        self.log.event("replay_started", capability=self.artifact.capability.id, version=self.artifact.capability.version,
                       inputs=sorted(self.inputs))
        if errors := validate_inputs(self.artifact, self.inputs):
            return self._finish(InvalidInput(run_id=self.run_id, completed_at=now(), errors=errors))
        if self.artifact.status != "approved" and not self.allow_draft:
            return self._finish(Failed(run_id=self.run_id, completed_at=now(), at_step="preflight",
                                       error_class="NOT_APPROVED", expected="status approved",
                                       observed=f"status {self.artifact.status}"))
        if self.audit.tripped(self.key):
            return self._finish(Failed(run_id=self.run_id, completed_at=now(), at_step="preflight",
                                       error_class="CIRCUIT_OPEN", expected="no recent wrong-record incidents",
                                       observed=f"{Audit.TRIP_AFTER}+ WRONG_RECORD in the last day; re-approve after "
                                                f"review to reset"))
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not self.headed)
            try:
                self.surface = await WebSurface.open(await browser.new_context(),
                                                     on_human=self._on_human if self.hub else None,
                                                     policy=self.artifact.policy)
                result = await self._run_steps()
            except _Stop as stop:
                result = stop.result
            except ControlLost as e:
                # Only an abort takes control away from a replay mid-action: it pauses itself for everything else
                result = Failed(run_id=self.run_id, completed_at=now(), at_step="control", error_class="OPERATOR_ABORTED",
                                expected="automation to hold control", observed=str(e), recovered=self.recovered,
                                drift=self.drift)
            finally:
                await browser.close()
        return self._finish(result)

    def _finish(self, result):
        self.log.event("replay_finished", status=result.status, result=result.model_dump(by_alias=True, exclude_none=True))
        self.log.write("result.json", result.model_dump_json(by_alias=True, exclude_none=True, indent=2))
        self.log.close()
        return result

    async def _run_steps(self):
        await self._navigate("preflight", self.artifact.app.entry_path)
        steps_done = False
        while True:
            try:
                if not steps_done:
                    for pre in self.artifact.preconditions:
                        if pre.unless and await self.surface.wait_any([pre.unless], 2_000) is not None:
                            self.log.event("precondition_skipped", subflow=pre.use)
                            continue
                        await self._run_flow(self.artifact.subflows[pre.use], recovering=False)
                    await self._run_flow(self.artifact.steps, recovering=False)
                    steps_done = True
                await self._checkpoint()
                break
            except _Restart:
                self.outputs.clear()
                steps_done = False
            except _Retry:
                continue  # a recovery or a person dealt with what the checkpoint met: check again
        return Success(run_id=self.run_id, completed_at=now(), outputs=self.outputs,
                       recovered=self.recovered, drift=self.drift)

    async def _run_flow(self, steps: list[Step], recovering: bool):
        for i, step in enumerate(steps):
            if not recovering:
                self._next = steps[i + 1] if i + 1 < len(steps) else None
            while True:
                try:
                    await self._run_step(step, recovering)
                    break
                except _Retry as retry:
                    if retry.step_done:
                        break

    # ── One step ───────────────────────────────────────────────────────────────────────────────────────────────

    async def _run_step(self, step: Step, recovering: bool):
        await self._outcomes(step, "before", recovering)
        timeout = step.wait_for.timeout_ms if step.wait_for else DEFAULT_TIMEOUT_MS

        if step.action == "navigate":
            await self._navigate(step.id, self._render(step.id, step.value))
        elif step.action == "extract":
            for name, extractor in step.extract.items():
                hit = await self._resolve(step, extractor, timeout, recovering)
                await self._identity_gate(step.id)
                text = await self.surface.read_text(hit.handle)
                self.outputs[name] = parse_value(text, extractor.parse)
                # The caller gets the value; the run log and evidence on disk only the placeholder
                for shown in {text.strip(), str(self.outputs[name])}:
                    self.log.mask(shown, f"{{{{outputs.{name}}}}}")
            self.log.event("extracted", step=step.id, outputs=sorted(step.extract))
        else:
            value = self._render(step.id, step.value) if step.value is not None else None
            hit = await self._act(step, step.target, step.action, value, timeout, recovering, check_policy=True)
            self.log.event("acted", step=step.id, action=step.action, value=step.value, rank=hit.rank, strategy=hit.strategy)

        if step.wait_for and step.wait_for.any_of:
            await self._wait_branches(step, recovering)
        elif step.wait_for and step.wait_for.load_state:
            await self.surface.wait_any([], timeout, step.wait_for.frame, step.wait_for.load_state)
        await self._outcomes(step, "after", recovering)
        if step.expect:
            # Race the postcondition against every outcome that could explain its absence, so an error page is
            # reported as soon as it shows, not after the timeout
            watched = self._watched(step)
            index = await self.surface.wait_any([step.expect, *(o.detect for o in watched)], timeout)
            if index:
                await self._handle(watched[index - 1], step, recovering, acted=True)
            if index is None:
                await self._outcomes(step, "after", recovering)
                if self.hub and not recovering:
                    await self._escalate(step, f"EXPECT_FAILED: '{step.intent}' didn't produce the expected screen",
                                         acted=True)
                raise self._fail(step.id, "EXPECT_FAILED", _describe(step.expect), "not on screen",
                                 evidence=await self._evidence(step.id))

    async def _act(self, step: Step, target, action: str, value: str | None, timeout: int, recovering: bool,
                   check_policy: bool = False) -> Resolved:
        """Resolve, then act. A transient failure (the element went stale while the frame re-rendered) gets one
        retry on a freshly resolved element; anything else stops the run. The control epoch is captured when the
        decision to act is made and checked again just before the input is dispatched."""
        for attempt in (1, 2):
            epoch = self.session.epoch
            hit = await self._resolve(step, target, timeout, recovering)
            await self._identity_gate(step.id)
            if check_policy:
                await self._check_policy(step, hit)
            mark = len(self.surface.blocked)
            try:
                await self.surface.act(hit.handle, action, value, guard=lambda: self.session.check(epoch))
                if refusals := self.surface.refusals_since(mark):
                    # The click sent the browser somewhere the policy forbids; the network layer dropped it
                    raise self._fail(step.id, "POLICY_VIOLATION", f"a {step.risk} action", refusals[0],
                                     evidence=await self._evidence(step.id))
                return hit
            except SurfaceError as e:
                if e.retry != "safe" or attempt == 2:
                    raise self._fail(step.id, "SURFACE_ERROR", f"{action} on the target", str(e))
                await asyncio.sleep(0.5)

    def _watched(self, step: Step) -> list[Outcome]:
        return [o for o in self.artifact.outcomes if o.at in ("any", f"after:{step.id}")]

    async def _resolve(self, step: Step, target, timeout: int, recovering: bool) -> Resolved:
        # Short resolve attempts with an outcome check between them: a declared outcome (an error page, "no member
        # found") ends the wait at once instead of after the full timeout
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            hit = await self.surface.resolve(target, min(1_000, timeout))
            if isinstance(hit, Resolved) or asyncio.get_running_loop().time() >= deadline:
                break
            await self._outcomes(step, "during", recovering)  # only outcomes declared for any point match
        if isinstance(hit, Resolved):
            # The control can appear on the next screen before anything else does, after the "before" check ran on
            # the last one: check again on the screen it was found on, so a non-modal dialog can't be clicked past
            await self._outcomes(step, "during", recovering)
            if hit.rank > 0:
                proposed = await self._propose_override(step, target, hit)
                self.drift.append(DriftEvent(step_id=step.id, hit_rank=hit.rank, strategy=hit.strategy,
                                             proposed_override=proposed))
                self.log.event("drift", step=step.id, rank=hit.rank, strategy=hit.strategy, proposed=proposed)
            return hit
        # Nothing found. A declared outcome (an error page, "no member found") explains that better than a miss.
        await self._outcomes(step, "after", recovering)
        ambiguous = any(code == "AMBIGUOUS_TARGET" for _, code in hit.attempts)
        if self.hub and not recovering:
            await self._escalate(step, f"{'AMBIGUOUS_TARGET' if ambiguous else 'TARGET_UNRESOLVED'}: can't find the "
                                       f"control for '{step.intent}'", acted=False)
        raise self._fail(step.id, "AMBIGUOUS_TARGET" if ambiguous else "TARGET_UNRESOLVED",
                         f"exactly one match for: {', '.join(k for k, _ in hit.attempts)}",
                         "; ".join(f"{k}: {c}" for k, c in hit.attempts), evidence=await self._evidence(step.id))

    async def _propose_override(self, step: Step, target, hit: Resolved) -> str | None:
        """Drift on a tenant becomes a pending override for that tenant, for a person to approve (§4 drift). Without
        a tenant there's nobody to propose it for: the drift is reported and that's all."""
        if not self.tenant:
            return None
        where = "target" if target is step.target else next(
            f"extract.{name}" for name, extractor in step.extract.items() if extractor is target)
        live = await hit.handle.evaluate(LIVE_NAME_JS)
        ops = overrides.propose(where, [s.model_dump(by_alias=True, exclude_none=True) for s in target.strategies],
                                hit.rank, live)
        path = overrides.write_pending(self.pending_dir / f"{self.tenant}.json", self.base,
                                       [op.model_copy(update={"step": step.id}) for op in ops])
        return str(path)

    async def _check_policy(self, step: Step, hit: Resolved):
        """The recorded risk class is checked again against the live control, so a relabelled button that now
        reads "Confirm" is still blocked."""
        name = await hit.handle.evaluate("el => (el.value || el.innerText || el.getAttribute('aria-label') || '').trim()")
        role = "button" if step.action == "click" else ""
        try:
            check_action(self.artifact.policy, step.action, role, name)
        except PolicyViolation as e:
            raise self._fail(step.id, "POLICY_VIOLATION", f"a {step.risk} action", str(e))

    async def _wait_branches(self, step: Step, recovering: bool):
        outcomes = {o.code: o for o in self.artifact.outcomes}
        conditions = [outcomes[b.code].detect if isinstance(b, OutcomeSeen) else b for b in step.wait_for.any_of]
        named = {b.code for b in step.wait_for.any_of if isinstance(b, OutcomeSeen)}
        others = [o for o in self._watched(step) if o.code not in named]
        index = await self.surface.wait_any([*conditions, *(o.detect for o in others)], step.wait_for.timeout_ms,
                                            step.wait_for.frame)
        if index is not None and index >= len(conditions):
            await self._handle(others[index - len(conditions)], step, recovering, acted=True)
        if index is None:
            await self._outcomes(step, "after", recovering)
            if self.hub and not recovering:
                await self._escalate(step, f"TIMEOUT: none of the screens expected after '{step.intent}' appeared",
                                     acted=True)
            raise self._fail(step.id, "TIMEOUT", "one of: " + ", ".join(_describe(c) for c in conditions),
                             f"none within {step.wait_for.timeout_ms} ms", evidence=await self._evidence(step.id))
        branch = step.wait_for.any_of[index]
        if isinstance(branch, OutcomeSeen):
            await self._handle(outcomes[branch.code], step, recovering, acted=True)

    # ── Outcomes ───────────────────────────────────────────────────────────────────────────────────────────────

    async def _outcomes(self, step: Step, when: str, recovering: bool):
        for outcome in self.artifact.outcomes:
            if outcome.at in ("any", f"{when}:{step.id}") and await self.surface.holds(outcome.detect):
                await self._handle(outcome, step, recovering, acted=when == "after")

    async def _handle(self, outcome: Outcome, step: Step, recovering: bool, acted: bool = False):
        self.log.event("outcome", code=outcome.code, kind=outcome.kind, step=step.id)
        match outcome.kind:
            case "business":
                raise _Stop(BusinessOutcome(run_id=self.run_id, completed_at=now(), code=outcome.code, at_step=step.id,
                                            recovered=self.recovered, drift=self.drift))
            case "fatal":
                raise self._fail(step.id, "FATAL_OUTCOME", "no error state", outcome.code, outcome_code=outcome.code,
                                 evidence=await self._evidence(step.id))
            case "escalate":
                if self.hub and not recovering:
                    await self._escalate(step, f"{outcome.code}: {outcome.description or 'needs a person'}", acted,
                                         outcome=outcome)
                raise self._fail(step.id, "ESCALATION_UNATTENDED", "an operator to take over", outcome.code,
                                 outcome_code=outcome.code, evidence=await self._evidence(step.id))
            case "recoverable":
                if recovering:
                    return  # a recovery's own steps don't trigger recoveries; the step that started it retries
                await self._recover(outcome, step, acted)

    async def _recover(self, outcome: Outcome, step: Step, acted: bool):
        self._recoveries[outcome.code] += 1
        attempt = self._recoveries[outcome.code]
        if attempt > outcome.recovery.max_attempts or len(self.recovered) >= MAX_RECOVERIES_PER_RUN:
            raise self._fail(step.id, "RECOVERY_EXHAUSTED", f"{outcome.code} to clear within its attempts",
                             f"{outcome.code} recurred (attempt {attempt}, {len(self.recovered)} recoveries this run)",
                             outcome_code=outcome.code, evidence=await self._evidence(step.id))
        match outcome.recovery.do:
            case RunSubflow(subflow=name):
                action = f"ran subflow {name}"
                await self._run_flow(self.artifact.subflows[name], recovering=True)
            case ClickThrough(target=target):
                action = "clicked through"
                await self._act(step, target, "click", None, DEFAULT_TIMEOUT_MS, recovering=True)
                await self._wait_cleared(outcome)
            case WaitLonger(timeout_ms=ms):
                action = f"waited {ms} ms"
                await asyncio.sleep(ms / 1000)
        self.recovered.append(RecoveryEvent(code=outcome.code, step_id=step.id, attempt=attempt, action=action))
        self.log.event("recovered", code=outcome.code, step=step.id, attempt=attempt, action=action,
                       resume=outcome.recovery.resume)
        raise _Restart() if outcome.recovery.resume == "start" else _Retry(step_done=acted)

    async def _wait_cleared(self, outcome: Outcome):
        """After clicking through, wait for the page to move on, so the same notice isn't met twice. If it's still
        there at the deadline the run carries on, and the recovery cap decides."""
        deadline = asyncio.get_running_loop().time() + DEFAULT_TIMEOUT_MS / 1000
        while await self.surface.holds(outcome.detect) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

    # ── Checkpoint ─────────────────────────────────────────────────────────────────────────────────────────────

    async def _checkpoint(self):
        for assertion in self.artifact.success.checkpoint:
            match assertion:
                case Visible():
                    # The last step's response may be a declared outcome (a form back with "minimum deposit"), not
                    # the success screen: race them, as every other wait does
                    last = self.artifact.steps[-1]
                    watched = self._watched(last)
                    index = await self.surface.wait_any([assertion, *(o.detect for o in watched)], 5_000)
                    if index:
                        await self._handle(watched[index - 1], last, recovering=False, acted=True)
                    if index is None:
                        raise self._fail("checkpoint", "CHECKPOINT_FAILED", _describe(assertion), "not on screen",
                                         evidence=await self._evidence("checkpoint"))
                case OutputPresent(output=name):
                    if assertion.not_empty and self.outputs.get(name) in (None, ""):
                        raise self._fail("checkpoint", "CHECKPOINT_FAILED", f"output {name}", "empty")
                case IdentityMatches():
                    hit = await self.surface.resolve(assertion.target, 5_000)
                    if not isinstance(hit, Resolved):
                        raise self._fail("checkpoint", "CHECKPOINT_FAILED", f"the {assertion.input} on screen",
                                         "not found", evidence=await self._evidence("checkpoint"))
                    shown = await self.surface.read_text(hit.handle)
                    if shown.strip() != self._render("checkpoint", assertion.equals).strip():
                        await self._wrong_record("checkpoint", assertion, shown)
        self.log.event("checkpoint_passed")

    async def _identity_gate(self, step_id: str):
        """D16, enforced before the first touch: once the page shows a record's identifier, no step reads from it or
        acts on it until that identifier matches the input. Runs after the step's target resolved, so the page it
        checks is the page the step is about to use. A page without the identifier (sign-on, search form) passes."""
        for assertion in self.artifact.success.checkpoint:
            if not isinstance(assertion, IdentityMatches):
                continue
            hit = await self.surface.resolve(assertion.target, 0)
            if not isinstance(hit, Resolved):
                continue
            shown = await self.surface.read_text(hit.handle)
            if shown.strip() != self._render(step_id, assertion.equals).strip():
                await self._wrong_record(step_id, assertion, shown)

    async def _wrong_record(self, step_id: str, assertion: IdentityMatches, shown: str):
        """Someone else's record is on screen: a security incident, not a retryable fault. Nothing read from it is
        returned, the evidence keeps the page's structure but none of its data, the caller isn't told whose record
        it was, and the audit log feeds the circuit breaker. No recovery or retry applies."""
        self.outputs.clear()
        self.audit.record("wrong_record", self.key, run_id=self.run_id, step=step_id, input=assertion.input,
                          observed_last4=last4(shown))
        self.log.event("identity_mismatch", step=step_id, input=assertion.input, observed_last4=last4(shown))
        raise self._fail(step_id, "WRONG_RECORD", f"{assertion.input} = {assertion.equals}",
                         f"a different {self.artifact.inputs[assertion.input].identifies or 'record'}",
                         evidence=await self._evidence(step_id, scrub=True))

    # ── Handing over to a person (§5) ──────────────────────────────────────────────────────────────────────────

    async def _escalate(self, step: Step, reason: str, acted: bool, outcome: Outcome | None = None):
        """Pause, hand the live session to an operator, and continue only once the screen proves it's safe to.
        Ends by raising _Retry (carry on) or _Stop (aborted, or nobody came)."""
        observation = await self.surface.observe()
        intervention = Intervention(
            id=new_run_id("int"), run_id=self.run_id, subject=self.key, step_id=step.id, intent=step.intent,
            reason=reason, since=now(), snapshot=self.log.redact(observation.text),
            screenshot=await self.surface.screenshot(self.log.path(f"{step.id}.intervention.png")))
        self.log.write(f"{intervention.id}.json", json.dumps({**_public(intervention), "snapshot": "(see .png)"}, indent=2))
        self.session.pause(intervention)
        self.hub.publish(intervention, self.session, self.surface)
        while True:
            state = await self.session.wait_for_hand_back(self.pause_timeout_s)
            if state == "aborted":
                self.hub.close(intervention.id, "aborted")
                raise self._fail(step.id, "OPERATOR_ABORTED", "the operator to hand back", "the operator aborted the run")
            if state == "timeout":
                self.hub.close(intervention.id, "timed out")
                raise self._fail(step.id, "ESCALATION_UNATTENDED", "an operator to take over",
                                 f"nobody took over within {self.pause_timeout_s:.0f} s")
            if verdict := await self._resume_check(step, acted, outcome):
                self.session.resume_verified()
                self.hub.close(intervention.id, f"resumed: {verdict}")
                self.log.event("resumed", step=step.id, verdict=verdict,
                               human_actions=len(intervention.human_actions))
                raise _Retry(step_done=verdict != "retry step")
            self.session.resume_rejected("resume check failed: the screen doesn't show the step done or ready to do")

    async def _resume_check(self, step: Step, acted: bool, outcome: Outcome | None) -> str | None:
        """Resume verifies, it doesn't assume. The outcome that paused the run must be gone; then either this step can
        run (its control is on screen), or the person already did it (the next step's control is on screen)."""
        await self.surface.observe()  # let the screen settle after the person's last action
        if outcome and await self.surface.holds(outcome.detect):
            return None
        if acted:
            return "step had already acted"
        if await self._ready(step):
            return "retry step"
        if self._next and await self._ready(self._next):
            return "operator did the step"
        return None

    async def _ready(self, step: Step) -> bool:
        targets = list(step.extract.values()) if step.extract else [step.target] if step.target else []
        for target in targets:
            if not isinstance(await self.surface.resolve(target, 1_500), Resolved):
                return False
        return bool(targets)

    async def _on_human(self, action: dict):
        """What a person did while holding control. In a replay it's evidence only: the artifact never changes."""
        if not self.session.human_in_control():
            return  # the page reports automation's own clicks too
        target = action["target"]
        self.session.record_human({
            "action": action["action"], "role": action["role"],
            "name": re.sub(r"\d", "#", self.log.redact(action["name"])),
            "target": target.model_dump(by_alias=True, exclude_none=True) if target else None,
            "typed": action["value"] is not None,  # never the value itself
        })

    # ── Helpers ────────────────────────────────────────────────────────────────────────────────────────────────

    def _render(self, step_id: str, text: str) -> str:
        try:
            rendered = render(text, self.inputs, self.env)
        except TemplateError as e:
            raise self._fail(step_id, "SURFACE_ERROR", "every template to have a value", str(e))
        for scope, name in TEMPLATE.findall(text):
            if scope == "secrets":
                self.log.mask(render(f"{{{{secrets.{name}}}}}", {}, self.env), f"{{{{secrets.{name}}}}}")
        return rendered

    async def _navigate(self, step_id: str, path_or_url: str):
        url = self.base_url + path_or_url if path_or_url.startswith("/") else path_or_url
        try:
            check_url(self.artifact.policy, url)
        except PolicyViolation as e:
            raise self._fail(step_id, "POLICY_VIOLATION", "an allowed URL", str(e))
        await self.surface.navigate(url)
        self.log.event("navigated", step=step_id, path=path_or_url)

    async def _evidence(self, label: str, scrub: bool = False) -> list[EvidenceRef]:
        """What the screen looked like when the run stopped: the redacted accessibility snapshot and a screenshot with
        PII blacked out. scrub is for a screen showing someone else's record: structure only, and no screenshot at
        all, since masking is detector-based and a miss there would store a third person's data."""
        refs = []
        try:
            observation = await self.surface.observe()
            text = structure_only(observation.text) if scrub else observation.text
            refs.append(EvidenceRef(kind="a11y_snapshot", path=self.log.write(f"{label}.snapshot.txt", text)))
            if not scrub:
                if shot := await self.surface.screenshot(self.log.path(f"{label}.png")):
                    refs.append(EvidenceRef(kind="screenshot", path=shot))
        except Exception as e:  # evidence is best effort; the failure itself is what matters
            self.log.event("evidence_failed", error=str(e))
        return refs


def _describe(condition) -> str:
    parts = [f"{k}={v!r}" for k, v in condition.model_dump(exclude_none=True, exclude={"kind"}).items()]
    return "visible(" + ", ".join(parts) + ")"
