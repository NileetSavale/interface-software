"""Turns what the model did into an artifact, one action at a time.

Each step's target is described from the live page at the moment of the action, never from the transcript, so
nothing the model saw or said ends up in the artifact (§2: the artifact is decoupled from the history).
"""
import re
from datetime import datetime, timezone

from interfaze.artifact.schema import (
    App, Artifact, Capability, Extractor, IdentityMatches, InputSpec, OutputPresent, OutputSpec, Policy, Provenance,
    Step, Success, Target, Visible,
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

    def build(self, success: Visible) -> Artifact:
        """Raises ValidationError when the recording breaks a schema rule; the loop shows that to the model."""
        goal = self.goal
        checkpoint = [success, *(OutputPresent(output=o) for o in self.outputs), *self.identity]
        return Artifact(
            capability=Capability(id=goal["capability"], version="1.0.0", title=goal["title"],
                                  description=goal["description"]),
            app=App(product=goal["product"], version_range=goal.get("versionRange", "*"), surface="web",
                    entry_path=goal.get("entryPath", "/")),
            status="draft",
            provenance=Provenance(discovery_run_id=self.run_id, model=self.model,
                                  recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  recorded_on_variant=goal.get("variant")),
            inputs={k: InputSpec.model_validate(v) for k, v in goal.get("inputs", {}).items()},
            outputs=self.outputs,
            policy=Policy(allowed_origins=[self.origin], max_risk=goal.get("maxRisk", "read")),
            steps=self.steps,
            success=Success(checkpoint=checkpoint),
        )
