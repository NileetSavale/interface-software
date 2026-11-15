"""Tenant overrides (docs/ARCHITECTURE.md §7, D8): how one bank's copy of a capability differs from the base.

An override names steps by id, never by position, so a step added to the base can't make it edit the wrong one, and
a step it names that no longer exists fails loudly. It declares the base versions it was written against
(`appliesTo`, a semver range); outside that range it isn't applied silently, it's refused until re-targeted.

Overrides come from drift: a replay on a tenant that only found a control through a fallback strategy writes a
*pending* override for that tenant (propose()). A person approves it into the artifact; nothing applies itself.
"""
import json
import re
from pathlib import Path

from interface.artifact.schema import Artifact, Override, SetOp

_COMPARATOR = re.compile(r"(>=|<=|>|<|=|\^|~)?\s*v?(\d+(?:\.\d+){0,2}|\*|x)")


class OverrideError(Exception):
    pass


def _version(text: str) -> tuple[int, int, int]:
    parts = [int(p) for p in text.split(".")]
    return tuple((parts + [0, 0])[:3])


def satisfies(version: str, spec: str) -> bool:
    """A small semver range check: space-separated comparators (">=1.0 <2.0"), caret ("^1.2.0"), tilde, "*"."""
    v = _version(version)
    for op, bound in _COMPARATOR.findall(spec):
        if bound in ("*", "x"):
            continue
        b = _version(bound)
        match op:
            case "^":
                ok = b <= v < ((b[0] + 1, 0, 0) if b[0] else (0, b[1] + 1, 0))
            case "~":
                ok = b <= v < (b[0], b[1] + 1, 0)
            case ">=": ok = v >= b
            case "<=": ok = v <= b
            case ">": ok = v > b
            case "<": ok = v < b
            case _: ok = v == b
        if not ok:
            return False
    return True


def apply(artifact: Artifact, tenant: str) -> Artifact:
    """The artifact as this tenant runs it. No override for the tenant: the base, unchanged."""
    override = artifact.overrides.get(tenant)
    if override is None:
        return artifact
    version = artifact.capability.version
    if not satisfies(version, override.applies_to):
        raise OverrideError(f"override for {tenant} applies to {override.applies_to}, but the artifact is {version}: "
                            f"re-target it before this tenant runs the new version")
    data = artifact.model_dump(by_alias=True, exclude_none=True)
    flows = [data["steps"], *data.get("subflows", {}).values()]
    for op in override.ops:
        flow, index = _find(flows, op.step, tenant)
        match op.op:
            case "set":
                _set(flow[index], op.path, op.value, f"override {tenant} on step {op.step}")
            case "insertBefore" | "insertAfter":
                flow.insert(index + (op.op == "insertAfter"), op.value.model_dump(by_alias=True, exclude_none=True))
            case "remove":
                del flow[index]
    data["overrides"] = {}  # applied: what runs is the tenant's own version
    return Artifact.model_validate(data)


def _find(flows: list[list[dict]], step_id: str, tenant: str) -> tuple[list[dict], int]:
    for flow in flows:
        for i, step in enumerate(flow):
            if step["id"] == step_id:
                return flow, i
    raise OverrideError(f"override {tenant} names step {step_id}, which the artifact doesn't have")


def _set(node: dict, path: str, value, where: str):
    """path is dotted, with list items picked by value: target.strategies[kind=role].name"""
    parts = re.findall(r"(\w+)(?:\[(\w+)=([^\]]+)\])?", path)
    for n, (key, pick_key, pick_value) in enumerate(parts):
        last = n == len(parts) - 1
        if key not in node and not last:
            raise OverrideError(f"{where}: {path} doesn't exist ({key})")
        if pick_key:
            items = [item for item in node[key] if str(item.get(pick_key)) == pick_value]
            if len(items) != 1:
                raise OverrideError(f"{where}: {path} picks {len(items)} items, needs exactly one")
            node = items[0]
            if last:
                raise OverrideError(f"{where}: {path} ends on a list item; set one of its fields")
        elif last:
            node[key] = value
        else:
            node = node[key]


def propose(target_path: str, strategies: list[dict], hit_rank: int, live_name: str | None) -> list[SetOp]:
    """What would make the recorded first strategy hit again, given what the fallback found on this tenant.

    If the first strategy is role+name and the control still has a usable name, keep it first and correct the name
    (and the matching text strategy): the control was relabelled. Otherwise move the strategy that worked to the
    front: the control lost what the first one relied on (a <label>, say)."""
    first = strategies[0]
    name = (live_name or "").strip()
    if first["kind"] == "role" and name and not re.search(r"\d", name):
        ops = [SetOp(step="", path=f"{target_path}.strategies[kind=role].name", value=name)]
        if any(s["kind"] == "text" for s in strategies):
            ops.append(SetOp(step="", path=f"{target_path}.strategies[kind=text].text", value=name))
        return ops
    reordered = [strategies[hit_rank], *strategies[:hit_rank], *strategies[hit_rank + 1:]]
    return [SetOp(step="", path=f"{target_path}.strategies", value=reordered)]


def write_pending(path: Path, artifact: Artifact, ops: list[SetOp]) -> Path:
    """Add ops to the tenant's pending override, replacing any earlier proposal for the same step and path."""
    current = Override.model_validate(json.loads(path.read_text())) if path.exists() else None
    kept = [op for op in (current.ops if current else []) if (op.step, getattr(op, "path", None))
            not in {(o.step, o.path) for o in ops}]
    major = _version(artifact.capability.version)[0]
    pending = Override(applies_to=f">={major}.0.0 <{major + 1}.0.0", ops=[*kept, *ops])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pending.model_dump_json(by_alias=True, exclude_none=True, indent=2) + "\n")
    return path
