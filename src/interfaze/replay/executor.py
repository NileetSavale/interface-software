"""Deterministic replay: an artifact plus inputs, run with no model in the loop (docs/ARCHITECTURE.md §4).

Per step: check outcomes → resolve the target → policy → act → wait → check outcomes → check the postcondition.
Same inputs, same steps: the only branching is on outcomes the artifact declares. Recoveries happen inside the run
and are reported beside the result; drift (a fallback strategy hit) never fails a run, it's reported.
"""
import asyncio
import re
from collections import Counter

from playwright.async_api import async_playwright

from interfaze.artifact.schema import (
    Artifact, ClickThrough, IdentityMatches, InputSpec, OutcomeSeen, Outcome, OutputPresent, RunSubflow, Step,
    TEMPLATE, Visible, WaitLonger,
)
from interfaze.artifact.templates import TemplateError, render
from interfaze.evidence import RunLog, new_run_id, now
from interfaze.policy import PolicyViolation, check_action, check_url
from interfaze.replay.result import (
    BusinessOutcome, DriftEvent, EvidenceRef, Failed, FailureClass, InputError, InvalidInput, RecoveryEvent, Success,
)
from interfaze.surface.base import Resolved, SurfaceError
from interfaze.surface.web import WebSurface

DEFAULT_TIMEOUT_MS = 10_000
MAX_RECOVERIES_PER_RUN = 3


class _Stop(Exception):
    """Ends the run with a result. Raised from deep inside a step so each check doesn't thread a return value."""
    def __init__(self, result):
        self.result = result


class _Retry(Exception):
    """A recovery ran; do the current step again."""


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
                 allow_draft: bool = False, env: dict[str, str] | None = None, run_id: str | None = None):
        self.artifact = artifact
        self.base_url = base_url.rstrip("/")
        self.inputs = {k: str(v) for k, v in inputs.items()}
        self.headed = headed
        self.allow_draft = allow_draft
        self.env = env
        self.run_id = run_id or new_run_id("replay")
        self.outputs: dict[str, object] = {}
        self.recovered: list[RecoveryEvent] = []
        self.drift: list[DriftEvent] = []
        self._recoveries: Counter[str] = Counter()
        self.surface: WebSurface | None = None
        self.log = RunLog(self.run_id)
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
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not self.headed)
            try:
                self.surface = await WebSurface.open(await browser.new_context())
                result = await self._run_steps()
            except _Stop as stop:
                result = stop.result
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
        for pre in self.artifact.preconditions:
            if pre.unless and await self.surface.wait_any([pre.unless], 2_000) is not None:
                self.log.event("precondition_skipped", subflow=pre.use)
                continue
            await self._run_flow(self.artifact.subflows[pre.use], recovering=False)
        await self._run_flow(self.artifact.steps, recovering=False)
        await self._checkpoint()
        return Success(run_id=self.run_id, completed_at=now(), outputs=self.outputs,
                       recovered=self.recovered, drift=self.drift)

    async def _run_flow(self, steps: list[Step], recovering: bool):
        for step in steps:
            while True:
                try:
                    await self._run_step(step, recovering)
                    break
                except _Retry:
                    continue

    # ── One step ───────────────────────────────────────────────────────────────────────────────────────────────

    async def _run_step(self, step: Step, recovering: bool):
        await self._outcomes(step, "before", recovering)
        timeout = step.wait_for.timeout_ms if step.wait_for else DEFAULT_TIMEOUT_MS

        if step.action == "navigate":
            await self._navigate(step.id, self._render(step.id, step.value))
        elif step.action == "extract":
            for name, extractor in step.extract.items():
                hit = await self._resolve(step, extractor, timeout, recovering)
                text = await self.surface.read_text(hit.handle)
                self.outputs[name] = parse_value(text, extractor.parse)
                # The caller gets the value; the run log and evidence on disk only the placeholder
                for shown in {text.strip(), str(self.outputs[name])}:
                    self.log.mask(shown, f"{{{{outputs.{name}}}}}")
            self.log.event("extracted", step=step.id, outputs=sorted(step.extract))
        else:
            hit = await self._resolve(step, step.target, timeout, recovering)
            await self._check_policy(step, hit)
            value = self._render(step.id, step.value) if step.value is not None else None
            try:
                await self.surface.act(hit.handle, step.action, value)
            except SurfaceError as e:
                if e.retry != "safe":
                    raise self._fail(step.id, "SURFACE_ERROR", f"{step.action} on the target", str(e))
                await asyncio.sleep(0.5)
                hit = await self._resolve(step, step.target, timeout, recovering)
                try:
                    await self.surface.act(hit.handle, step.action, value)
                except SurfaceError as e2:
                    raise self._fail(step.id, "SURFACE_ERROR", f"{step.action} on the target", str(e2))
            self.log.event("acted", step=step.id, action=step.action, value=step.value, rank=hit.rank, strategy=hit.strategy)

        if step.wait_for and step.wait_for.any_of:
            await self._wait_branches(step, recovering)
        elif step.wait_for and step.wait_for.load_state:
            await self.surface.wait_any([], timeout, step.wait_for.frame, step.wait_for.load_state)
        await self._outcomes(step, "after", recovering)
        if step.expect and await self.surface.wait_any([step.expect], timeout) is None:
            await self._outcomes(step, "after", recovering)  # an error page explains a missing postcondition better
            raise self._fail(step.id, "EXPECT_FAILED", _describe(step.expect), "not on screen", evidence=await self._evidence(step.id))

    async def _resolve(self, step: Step, target, timeout: int, recovering: bool) -> Resolved:
        hit = await self.surface.resolve(target, timeout)
        if isinstance(hit, Resolved):
            if hit.rank > 0:
                self.drift.append(DriftEvent(step_id=step.id, hit_rank=hit.rank, strategy=hit.strategy))
                self.log.event("drift", step=step.id, rank=hit.rank, strategy=hit.strategy)
            return hit
        # Nothing found. A declared outcome (an error page, "no member found") explains that better than a miss.
        await self._outcomes(step, "after", recovering)
        ambiguous = any(code == "AMBIGUOUS_TARGET" for _, code in hit.attempts)
        raise self._fail(step.id, "AMBIGUOUS_TARGET" if ambiguous else "TARGET_UNRESOLVED",
                         f"exactly one match for: {', '.join(k for k, _ in hit.attempts)}",
                         "; ".join(f"{k}: {c}" for k, c in hit.attempts), evidence=await self._evidence(step.id))

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
        index = await self.surface.wait_any(conditions, step.wait_for.timeout_ms, step.wait_for.frame)
        if index is None:
            await self._outcomes(step, "after", recovering)
            raise self._fail(step.id, "TIMEOUT", "one of: " + ", ".join(_describe(c) for c in conditions),
                             f"none within {step.wait_for.timeout_ms} ms", evidence=await self._evidence(step.id))
        branch = step.wait_for.any_of[index]
        if isinstance(branch, OutcomeSeen):
            await self._handle(outcomes[branch.code], step, recovering)

    # ── Outcomes ───────────────────────────────────────────────────────────────────────────────────────────────

    async def _outcomes(self, step: Step, when: str, recovering: bool):
        for outcome in self.artifact.outcomes:
            if outcome.at in ("any", f"{when}:{step.id}") and await self.surface.holds(outcome.detect):
                await self._handle(outcome, step, recovering)

    async def _handle(self, outcome: Outcome, step: Step, recovering: bool):
        self.log.event("outcome", code=outcome.code, kind=outcome.kind, step=step.id)
        match outcome.kind:
            case "business":
                raise _Stop(BusinessOutcome(run_id=self.run_id, completed_at=now(), code=outcome.code, at_step=step.id,
                                            recovered=self.recovered, drift=self.drift))
            case "fatal":
                raise self._fail(step.id, "FATAL_OUTCOME", "no error state", outcome.code, outcome_code=outcome.code,
                                 evidence=await self._evidence(step.id))
            case "escalate":
                # Pass 2: with an operator attached this becomes needs_human and the session is handed over
                raise self._fail(step.id, "ESCALATION_UNATTENDED", "an operator to take over", outcome.code,
                                 outcome_code=outcome.code, evidence=await self._evidence(step.id))
            case "recoverable":
                if recovering:
                    return  # a recovery's own steps don't trigger recoveries; the step that started it retries
                await self._recover(outcome, step)

    async def _recover(self, outcome: Outcome, step: Step):
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
                hit = await self._resolve(step, target, DEFAULT_TIMEOUT_MS, recovering=True)
                await self.surface.act(hit.handle, "click")
            case WaitLonger(timeout_ms=ms):
                action = f"waited {ms} ms"
                await asyncio.sleep(ms / 1000)
        self.recovered.append(RecoveryEvent(code=outcome.code, step_id=step.id, attempt=attempt, action=action))
        self.log.event("recovered", code=outcome.code, step=step.id, attempt=attempt, action=action)
        raise _Retry()

    # ── Checkpoint ─────────────────────────────────────────────────────────────────────────────────────────────

    async def _checkpoint(self):
        for assertion in self.artifact.success.checkpoint:
            match assertion:
                case Visible():
                    if await self.surface.wait_any([assertion], 5_000) is None:
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
                    wanted = self._render("checkpoint", assertion.equals)
                    if shown.strip() != wanted.strip():
                        # Never return a neighbouring record's data as success (D16). The observed value is shown
                        # last-4 only: it's someone else's identifier.
                        self.outputs.clear()
                        raise self._fail("checkpoint", "WRONG_RECORD", f"{assertion.input} = {assertion.equals}",
                                         f"{assertion.input} = {last4(shown)}", evidence=await self._evidence("checkpoint"))
        self.log.event("checkpoint_passed")

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

    async def _evidence(self, label: str) -> list[EvidenceRef]:
        """What the screen looked like when the run stopped: the redacted accessibility snapshot and a screenshot.
        (Screenshots aren't masked yet; that's Pass 2. The mock app's data is fake.)"""
        refs = []
        try:
            observation = await self.surface.observe()
            refs.append(EvidenceRef(kind="a11y_snapshot", path=self.log.write(f"{label}.snapshot.txt", observation.text)))
            refs.append(EvidenceRef(kind="screenshot", path=await self.surface.screenshot(self.log.path(f"{label}.png"))))
        except Exception as e:  # evidence is best effort; the failure itself is what matters
            self.log.event("evidence_failed", error=str(e))
        return refs


def _describe(condition) -> str:
    parts = [f"{k}={v!r}" for k, v in condition.model_dump(exclude_none=True, exclude={"kind"}).items()]
    return "visible(" + ", ".join(parts) + ")"
