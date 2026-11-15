"""Turns what the model did into an artifact, one action at a time.

Each step's target is described from the live page at the moment of the action, never from the transcript, so
nothing the model saw or said ends up in the artifact (§2: the artifact is decoupled from the history).
"""
import re
from datetime import datetime, timezone

from interfaze.artifact.library import SIGN_ON, standard_outcomes
from interfaze.artifact.schema import (
    App, Artifact, Capability, Extractor, IdentityMatches, InputSpec, LabelStrategy, OutputPresent, OutputSpec, Policy,
    Precondition, Provenance, RoleStrategy, Step, Success, Target, TextStrategy, Visible,
)

PARSE = {"string": "text", "money": "money", "integer": "integer", "date": "date"}


def step_id(intent: str, taken: set[str]) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", intent.lower()) if w not in {"the", "a", "an", "to", "on", "of", "and"}]
    base = "_".join(words[:5]) or "step"
    base = base if base[0].isalpha() else f"s_{base}"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate)
    return candidate


class Recorder:
    def __init__(self, goal: dict, origin: str, run_id: str, model: str):
        self.goal = goal
        self.origin = origin
        self.run_id = run_id
        self.model = model
        self.steps: list[Step] = []
        self.outputs: dict[str, OutputSpec] = {}
        self.identity: list[IdentityMatches] = []
        self._ids: set[str] = set()

    def act(self, intent: str, action: str, target: Target, value: str | None, risk: str, recorded_by: str = "model"):
        self.steps.append(Step(id=step_id(intent, self._ids), intent=intent, action=action, target=target, value=value,
                               risk=risk, recorded_by=recorded_by))

    def extract(self, intent: str, output: str, kind: str, target: Target):
        if output in self.outputs:
            raise ValueError(f"output {output} is already extracted; each output has exactly one source")
        self.outputs[output] = OutputSpec(type=kind)
        extractor = Extractor(**target.model_dump(), parse=PARSE[kind])
        self.steps.append(Step(id=step_id(intent, self._ids), intent=intent, action="extract",
                               extract={output: extractor}, risk="read"))

    def verify_identity(self, input_name: str, target: Target):
        self.identity.append(IdentityMatches(input=input_name, target=target, equals=f"{{{{inputs.{input_name}}}}}"))

    def _split_sign_on(self, entry_path: str) -> tuple[dict, list[Precondition], list[Step]]:
        """Signing on becomes its own subflow, run as a precondition: replay skips it when already signed on, and the
        SESSION_EXPIRED recovery reuses it. The sign-on steps are the ones up to the first click after the last
        credential is typed."""
        secret = [i for i, s in enumerate(self.steps) if s.value and "{{secrets." in s.value]
        submit = next((i for i, s in enumerate(self.steps) if secret and i > secret[-1] and s.action == "click"), None)
        if submit is None or submit + 1 >= len(self.steps):
            return {}, [], self.steps
        signed_on = _seen(self.steps[submit + 1].target)
        sign_on = [Step(id="open_sign_on_page", intent="open the sign-on page", action="navigate", value=entry_path,
                        risk="read", recorded_by="author"),
                   *self.steps[:submit],
                   self.steps[submit].model_copy(update={"expect": signed_on})]
        return {SIGN_ON: sign_on}, [Precondition(use=SIGN_ON, unless=signed_on)], self.steps[submit + 1:]

    def build(self, success: Visible) -> Artifact:
        """Raises ValidationError when the recording breaks a schema rule; the loop shows that to the model."""
        goal = self.goal
        checkpoint = [success, *(OutputPresent(output=o) for o in self.outputs), *self.identity]
        entry_path = goal.get("entryPath", "/")
        subflows, preconditions, steps = self._split_sign_on(entry_path)
        max_risk = goal.get("maxRisk", "read")
        return Artifact(
            capability=Capability(id=goal["capability"], version="1.0.0", title=goal["title"],
                                  description=goal["description"]),
            app=App(product=goal["product"], version_range=goal.get("versionRange", "*"), surface="web",
                    entry_path=entry_path),
            status="draft",
            provenance=Provenance(discovery_run_id=self.run_id, model=self.model,
                                  recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  recorded_on_variant=goal.get("variant")),
            inputs={k: InputSpec.model_validate(v) for k, v in goal.get("inputs", {}).items()},
            outputs=self.outputs,
            outcomes=standard_outcomes(has_sign_on=bool(subflows), read_only=max_risk == "read"),
            policy=Policy(allowed_origins=[self.origin], max_risk=max_risk),
            subflows=subflows,
            preconditions=preconditions,
            steps=steps,
            success=Success(checkpoint=checkpoint),
        )


def _seen(target: Target | None) -> Visible | None:
    """A condition that holds once the page shows this target: proof the step before it worked."""
    if target is None:
        return None
    for strategy in target.strategies:
        match strategy:
            case RoleStrategy(role=role, name=name):
                return Visible(frame=target.frame, role=role, name=name)
            case LabelStrategy(label=label):
                return Visible(frame=target.frame, label=label)
            case TextStrategy(text=text):
                return Visible(frame=target.frame, text=text)
    return None
