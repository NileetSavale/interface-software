"""The capability artifact: what discovery records and replay runs.

The top half (capability, inputs, outputs, outcomes, policy) is the contract a calling agent reads. The bottom half
(subflows, preconditions, steps, success) is how replay carries it out. Nothing here is Playwright-specific: targets
are surface-neutral descriptions that each Surface resolves its own way (docs/ARCHITECTURE.md §2).

Validation is strict on purpose. A rule that's only a convention gets broken; a rule the schema enforces doesn't.
"""
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = "1"

# {{inputs.memberId}} is supplied per invocation; {{secrets.APP_USER}} is read from env by the Session at act time
TEMPLATE = re.compile(r"\{\{\s*(inputs|secrets)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_STEP_ID = r"^[a-z][a-z0-9_]*$"
_CODE = r"^[A-Z][A-Z0-9_]*$"
_SEMVER = r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$"


class Model(BaseModel):
    """camelCase in JSON, snake_case in Python, and unknown fields are errors rather than silently ignored."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


def _compiles(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"not a valid regex: {e}") from e
    return pattern


class Regex(Model):
    matches: Annotated[str, AfterValidator(_compiles)]


# A plain string matches exactly (after trimming whitespace); {"matches": ...} is a regex search
TextMatch = str | Regex


# ── Locating a control ─────────────────────────────────────────────────────────────────────────────────────────────
# Strategies are listed in rank order inside a Target. Replay tries them in order and reports which one hit; a hit
# below rank 0 is drift (§4). A strategy that matches more than one control is never resolved by guessing.

class RoleStrategy(Model):
    """Rank 1: accessible role + name. Survives restyling and DOM changes."""
    kind: Literal["role"] = "role"
    role: str
    name: TextMatch


class LabelStrategy(Model):
    """Rank 2: the form control a label is associated with."""
    kind: Literal["label"] = "label"
    label: TextMatch


class AnchorStrategy(Model):
    """Rank 3: a control positioned relative to visible text. The legacy case: no labels, only a neighbouring cell,
    or a caption the control follows ("after": the nearest text before it in reading order is this one)."""
    kind: Literal["anchor"] = "anchor"
    anchor_text: TextMatch
    relation: Literal["right-of", "left-of", "above", "below", "after"]
    role: str | None = None


class RowWhere(Model):
    column: str
    equals: str = Field(description="Literal, or a template such as {{inputs.accountType}}.")


class TableCellStrategy(Model):
    """Rank 3: a cell picked by values, not position: the Balance column of the row where Type = Savings.
    The one exception is row "first" (D27): the first result in a results table, for reading only, and only when
    the goal asks for the first result. An empty table finds nothing; nothing else is read instead."""
    kind: Literal["tableCell"] = "tableCell"
    table_anchor: TextMatch = Field(description="Caption or heading text that identifies the table.")
    row_where: RowWhere | None = None
    row: Literal["first"] | None = Field(default=None, description="The first data row; reading only (D27).")
    column: str
    role: str | None = Field(default=None, description="Set for a control in the cell (the row's View link); "
                                                       "unset reads the cell itself.")

    @model_validator(mode="after")
    def _one_way_to_pick_the_row(self):
        if (self.row_where is None) == (self.row is None):
            raise ValueError("a table cell picks its row by rowWhere or row: first, exactly one")
        if self.row and self.role:
            raise ValueError("row: first is for reading a cell, never for acting on a control (D27)")
        return self


class TextStrategy(Model):
    """Rank 3: an element by its visible text alone."""
    kind: Literal["text"] = "text"
    text: TextMatch


class OnlyInRegionStrategy(Model):
    """Rank 4: the only control of this role inside the target's labelled region ("the one button in the Member
    Inquiry fieldset"). Survives a relabel, which every name-based strategy above it doesn't; it's recorded only
    when it was unique, and it fails rather than guesses once the region holds two."""
    kind: Literal["onlyInRegion"] = "onlyInRegion"
    role: str


class StructuralStrategy(Model):
    """Rank 4: a path through the frame's markup. Last DOM resort; breaks on any layout change."""
    kind: Literal["structural"] = "structural"
    path: str


class BBoxStrategy(Model):
    """Rank 5: a position in the frame. Used to break ties, and on desktop surfaces with thin trees."""
    kind: Literal["bbox"] = "bbox"
    x: float
    y: float
    width: float
    height: float


Strategy = Annotated[
    RoleStrategy | LabelStrategy | AnchorStrategy | TableCellStrategy | TextStrategy | OnlyInRegionStrategy
    | StructuralStrategy | BBoxStrategy,
    Field(discriminator="kind"),
]


class RegionScope(Model):
    """Narrows a target to a labelled region, so a duplicate name elsewhere on the page can't match (§2 ambiguity)."""
    role: str = Field(description="form, region, group, table, dialog…")
    name: TextMatch


class Target(Model):
    frame: list[str] = Field(default_factory=list, description="Frame path from the top document; [] is the top.")
    within: RegionScope | None = None
    strategies: list[Strategy] = Field(min_length=1)
    nth: int | None = Field(
        default=None, ge=0,
        description="Last resort when no scope tells duplicates apart. Requires the step to be flagged needsReview.",
    )

    @model_validator(mode="after")
    def _region_strategy_needs_a_region(self):
        if self.within is None and any(isinstance(s, OnlyInRegionStrategy) for s in self.strategies):
            raise ValueError("an onlyInRegion strategy needs the target's `within` region; page-wide it would guess")
        return self


# ── Conditions ─────────────────────────────────────────────────────────────────────────────────────────────────────

class Visible(Model):
    """Something is on screen. All given fields must match the same element (or, for text alone, any text)."""
    kind: Literal["visible"] = "visible"
    frame: list[str] | None = Field(default=None, description="None searches every frame.")
    role: str | None = None
    name: TextMatch | None = None
    label: TextMatch | None = None
    text: TextMatch | None = None

    @model_validator(mode="after")
    def _says_something(self):
        if not any([self.role, self.name, self.label, self.text]):
            raise ValueError("a visible condition needs at least one of role, name, label, text")
        return self


class OutcomeSeen(Model):
    """A declared outcome's detector matched. Used to branch a wait (e.g. results page OR 'no member found')."""
    kind: Literal["outcome"] = "outcome"
    code: str


class OutputPresent(Model):
    kind: Literal["output"] = "output"
    output: str
    not_empty: bool = True


class IdentityMatches(Model):
    """The record on screen is the one asked for. A mismatch is always the fatal WRONG_RECORD (D16): a prefix-matched
    neighbour's balance must never come back as success."""
    kind: Literal["identity"] = "identity"
    input: str
    target: Target
    equals: str = Field(description="Usually {{inputs.<input>}}.")


WaitBranch = Annotated[Visible | OutcomeSeen, Field(discriminator="kind")]
CheckpointAssertion = Annotated[Visible | OutputPresent | IdentityMatches, Field(discriminator="kind")]


class WaitFor(Model):
    """No fixed sleeps: a step waits until one of these holds, or times out (§4 determinism rules)."""
    frame: list[str] | None = None
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = None
    any_of: list[WaitBranch] = Field(default_factory=list)
    timeout_ms: int = Field(default=10_000, ge=100, le=120_000)


# ── Steps ──────────────────────────────────────────────────────────────────────────────────────────────────────────

Action = Literal["click", "type", "select", "check", "uncheck", "press", "navigate", "extract", "pageUntil"]


class Extractor(Target):
    parse: Literal["text", "money", "integer", "date"] = "text"


class Step(Model):
    id: Annotated[str, Field(pattern=_STEP_ID)]
    intent: str = Field(min_length=3, description="What the step is for, in words. Review, drift reports and repair key off it.")
    action: Action
    target: Target | None = None
    value: str | None = Field(default=None, description="Text to type/select, key to press, or URL. Templates allowed.")
    extract: dict[str, Extractor] | None = None
    risk: Literal["read", "reversible"] = Field(
        description="Irreversible steps can't exist in an artifact: they're blocked by policy (D1). An artifact can "
                    "reach a commit point but never press it.",
    )
    wait_for: WaitFor | None = None
    expect: Visible | None = Field(default=None, description="Postcondition. We never assume a click worked.")
    until: Target | None = Field(default=None, description="pageUntil: what must be on screen to stop turning pages.")
    max_pages: int | None = Field(default=None, ge=1, le=50, description="pageUntil: the most Next presses allowed.")
    recorded_by: Literal["model", "human", "author", "map"] = "model"
    needs_review: bool = False

    @model_validator(mode="after")
    def _shape_fits_action(self):
        # D28: the only loop an artifact can hold. Bounded and with a declared exit: press target (the Next control)
        # until `until` resolves to exactly one element, at most max_pages times
        if (self.action == "pageUntil") != (self.until is not None and self.max_pages is not None):
            raise ValueError(f"step {self.id}: pageUntil, and only it, carries until and maxPages")
        if self.action == "pageUntil" and self.target is None:
            raise ValueError(f"step {self.id}: pageUntil needs the Next control as its target")
        needs_target = {"click", "type", "select", "check", "uncheck"}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"step {self.id}: {self.action} needs a target")
        if self.action in {"type", "select", "press", "navigate"} and self.value is None:
            raise ValueError(f"step {self.id}: {self.action} needs a value")
        if self.action == "navigate" and self.target is not None:
            raise ValueError(f"step {self.id}: navigate takes a URL value, not a target")
        if (self.action == "extract") != bool(self.extract):
            raise ValueError(f"step {self.id}: extract steps, and only they, carry an extract map")
        if self.action == "extract" and self.target is not None:
            raise ValueError(f"step {self.id}: an extract step's targets go in its extract map")
        positional = [t for t in [self.target, *(self.extract or {}).values()] if t is not None and t.nth is not None]
        if positional and not self.needs_review:
            raise ValueError(f"step {self.id}: a target located by nth position must be flagged needsReview")
        return self


# ── Contract ───────────────────────────────────────────────────────────────────────────────────────────────────────

class Capability(Model):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$", description="namespace.name")]
    version: Annotated[str, Field(pattern=_SEMVER, description="A breaking change to inputs, outputs or outcomes is a major bump.")]
    title: str
    description: str


class App(Model):
    product: str = Field(description="The vendor product, not a tenant. Tenants specialise through overrides.")
    version_range: str
    surface: Literal["web", "terminal", "desktop"]
    entry_path: str = Field(
        default="/", description="Where replay starts, relative to the tenant's base URL (each bank hosts its own).",
    )


class Provenance(Model):
    discovery_run_id: str | None = None
    recorded_at: str
    model: str | None = None
    recorded_on_variant: str | None = None


class InputSpec(Model):
    type: Literal["string", "integer", "money", "date", "enum"]
    description: str
    pattern: Annotated[str, AfterValidator(_compiles)] | None = None
    values: list[str] | None = Field(default=None, description="Allowed values when type is enum.")
    sensitive: bool = False
    identifies: str | None = Field(
        default=None, description="Set when the input names a record (member, account). Requires an identity checkpoint.",
    )

    @model_validator(mode="after")
    def _enum_has_values(self):
        if (self.type == "enum") != bool(self.values):
            raise ValueError("enum inputs, and only they, list their values")
        return self


class OutputSpec(Model):
    type: Literal["string", "integer", "money", "date"]
    description: str | None = None
    redact: Literal["none", "partial", "full"] = Field(
        default="none", description="How the value appears in logs and evidence. The caller always gets it in full.",
    )


class RunSubflow(Model):
    kind: Literal["subflow"] = "subflow"
    subflow: str


class ClickThrough(Model):
    kind: Literal["click"] = "click"
    target: Target


class WaitLonger(Model):
    kind: Literal["wait"] = "wait"
    timeout_ms: int = Field(ge=100, le=120_000)


class Recovery(Model):
    """What to do, then where to pick up. A run allows at most 3 recoveries in total, whatever these say."""
    do: Annotated[RunSubflow | ClickThrough | WaitLonger, Field(discriminator="kind")]
    max_attempts: int = Field(default=1, ge=1, le=3)
    resume: Literal["step", "start"] = Field(
        default="step",
        description="step: retry the step that met the outcome. start: run the steps again from the first, for when "
                    "the recovery lands somewhere else (signing on again opens the home screen). Read-only "
                    "capabilities only: nothing that changed state may run twice.",
    )


class Outcome(Model):
    """A named state replay can recognise. business: a legitimate answer for the caller (no such member).
    recoverable: handled inside the run. fatal: stop with evidence. escalate: pause for a human (unknown dialog)."""
    code: Annotated[str, Field(pattern=_CODE)]
    kind: Literal["business", "recoverable", "fatal", "escalate"]
    at: Annotated[str, Field(pattern=r"^(any|(before|after):[a-z][a-z0-9_]*)$")] = "any"
    detect: Visible
    recovery: Recovery | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _recovery_only_when_recoverable(self):
        if (self.kind == "recoverable") != (self.recovery is not None):
            raise ValueError(f"outcome {self.code}: recoverable outcomes, and only they, declare a recovery")
        return self


class Policy(Model):
    allowed_origins: list[str] = Field(min_length=1)
    allowed_paths: list[str] = Field(default_factory=lambda: ["/*"], description="Glob patterns on the URL path.")
    max_risk: Literal["read", "reversible"] = "read"
    commit_paths: list[str] = Field(default_factory=list, description=(
        "URL paths (globs) that commit something, e.g. a confirm endpoint. Any request to one is blocked in the "
        "browser's network layer, whatever the button that sent it is called. From the app's integration config."))


class Precondition(Model):
    use: str = Field(description="A subflow to run first,")
    unless: Visible | None = Field(default=None, description="…skipped when this already holds (e.g. already logged in).")


class Success(Model):
    checkpoint: list[CheckpointAssertion] = Field(min_length=1)


# ── Tenant overrides (D8): addressed by step id, never by position ────────────────────────────────────────────────

class SetOp(Model):
    op: Literal["set"] = "set"
    step: str
    path: str = Field(description="Dotted path inside the step; a list item is picked by value: strategies[kind=role].name")
    value: object


class InsertOp(Model):
    op: Literal["insertBefore", "insertAfter"]
    step: str
    value: Step


class RemoveOp(Model):
    op: Literal["remove"] = "remove"
    step: str


class Override(Model):
    applies_to: str = Field(description="Semver range over capability.version. A breaking base change flags the override.")
    ops: list[Annotated[SetOp | InsertOp | RemoveOp, Field(discriminator="op")]] = Field(min_length=1)


# ── The artifact ───────────────────────────────────────────────────────────────────────────────────────────────────

class Artifact(Model):
    schema_version: Literal["1"] = SCHEMA_VERSION
    capability: Capability
    app: App
    status: Literal["draft", "approved", "deprecated"] = Field(
        default="draft", description="Only approved artifacts replay unattended.",
    )
    provenance: Provenance
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    outcomes: list[Outcome] = Field(default_factory=list)
    policy: Policy
    subflows: dict[str, list[Step]] = Field(default_factory=dict)
    preconditions: list[Precondition] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    success: Success
    overrides: dict[str, Override] = Field(default_factory=dict)

    def all_steps(self) -> list[Step]:
        return [*self.steps, *(s for flow in self.subflows.values() for s in flow)]

    @model_validator(mode="after")
    def _references_resolve(self):
        problems = []
        steps = self.all_steps()
        ids = [s.id for s in steps]
        problems += [f"step id {i} is used more than once" for i in sorted({i for i in ids if ids.count(i) > 1})]

        codes = [o.code for o in self.outcomes]
        problems += [f"outcome {c} is declared more than once" for c in sorted({c for c in codes if codes.count(c) > 1})]
        for o in self.outcomes:
            if o.at != "any" and o.at.split(":", 1)[1] not in ids:
                problems.append(f"outcome {o.code} is attached to unknown step {o.at.split(':', 1)[1]}")
            if isinstance(o.recovery and o.recovery.do, RunSubflow) and o.recovery.do.subflow not in self.subflows:
                problems.append(f"outcome {o.code} recovers with unknown subflow {o.recovery.do.subflow}")
            if o.recovery and o.recovery.resume == "start" and self.policy.max_risk != "read":
                problems.append(f"outcome {o.code} restarts the steps, which only a read-only capability may do")

        for s in steps:
            for branch in s.wait_for.any_of if s.wait_for else []:
                if isinstance(branch, OutcomeSeen) and branch.code not in codes:
                    problems.append(f"step {s.id} waits for undeclared outcome {branch.code}")
            if s.risk == "reversible" and self.policy.max_risk == "read":
                problems.append(f"step {s.id} is reversible but the policy allows read only")

        for p in self.preconditions:
            if p.use not in self.subflows:
                problems.append(f"precondition uses unknown subflow {p.use}")

        problems += self._check_templates(steps)
        problems += self._check_outputs(steps)
        problems += self._check_identity()

        for variant, override in self.overrides.items():
            problems += [f"override {variant} targets unknown step {op.step}" for op in override.ops if op.step not in ids]

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _check_templates(self, steps: list[Step]) -> list[str]:
        texts = [s.value for s in steps if s.value]
        texts += [c.equals for c in self.success.checkpoint if isinstance(c, IdentityMatches)]
        # Targets may name an input (the link {{inputs.accountId}}), filled in at replay; never a secret
        targets = [t.model_dump_json(by_alias=True) for s in steps for t in [s.target, s.until, *(s.extract or {}).values()]
                   if t]
        texts += targets
        missing = {name for text in texts for scope, name in TEMPLATE.findall(text) if scope == "inputs" and name not in self.inputs}
        secret = sorted({name for text in targets for scope, name in TEMPLATE.findall(text) if scope == "secrets"})
        return ([f"template refers to undeclared input {name}" for name in sorted(missing)]
                + [f"a target may not contain secret {name}" for name in secret])

    def _check_outputs(self, steps: list[Step]) -> list[str]:
        """Every declared output has exactly one extractor, and nothing is extracted that isn't declared."""
        extracted = [name for s in steps for name in (s.extract or {})]
        problems = [f"output {o} has no extractor" for o in self.outputs if o not in extracted]
        problems += [f"output {o} is extracted more than once" for o in sorted({o for o in extracted if extracted.count(o) > 1})]
        problems += [f"extractor {o} isn't a declared output" for o in extracted if o not in self.outputs]
        refs = [c.output for c in self.success.checkpoint if isinstance(c, OutputPresent)]
        return problems + [f"checkpoint refers to undeclared output {o}" for o in refs if o not in self.outputs]

    def _check_identity(self) -> list[str]:
        """D16: an input that names a record must be checked against the record on screen."""
        checked = {c.input for c in self.success.checkpoint if isinstance(c, IdentityMatches)}
        problems = [f"input {name} identifies a {spec.identifies} but no identity checkpoint verifies it"
                    for name, spec in self.inputs.items() if spec.identifies and name not in checked]
        return problems + [f"identity checkpoint refers to undeclared input {name}" for name in sorted(checked - set(self.inputs))]
