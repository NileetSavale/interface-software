"""Filling {{inputs.x}} and {{secrets.X}} at act time. Values never go back into the artifact or the logs.

Where secrets come from is a Mapping (name -> value): the process environment by default, and any vault client that
can be read as a mapping without changing anything else. Each bank has its own operator, so a tenant's secrets are
looked up under that tenant's name only (`APP_USER__B`), never falling back to another bank's (D26).
"""
import json
import os
import re
from collections.abc import Iterator, Mapping

from interface.artifact.schema import TEMPLATE


class TemplateError(ValueError):
    pass


class TenantSecrets(Mapping):
    """A tenant's view of a secret source: {{secrets.APP_USER}} on bank B is APP_USER__B, and nothing else."""

    def __init__(self, source: Mapping[str, str], tenant: str):
        self.source, self.suffix = source, f"__{tenant.upper()}"

    def key(self, name: str) -> str:
        return name + self.suffix

    def __getitem__(self, name: str) -> str:
        return self.source[self.key(name)]

    def __iter__(self) -> Iterator[str]:
        return (k.removesuffix(self.suffix) for k in self.source if k.endswith(self.suffix))

    def __len__(self) -> int:
        return sum(1 for _ in self)


def secrets_for(source: Mapping[str, str] | None, tenant: str | None) -> Mapping[str, str] | None:
    if tenant is None:
        return source
    return TenantSecrets(os.environ if source is None else source, tenant)


_INPUT = re.compile(r"\{\{\s*inputs\.(\w+)\s*\}\}")


def bind(target, inputs: dict[str, str]):
    """A target with this run's inputs in it: the link named {{inputs.accountId}} becomes the link named 14010.
    Inputs only; a secret is never written into anything a page or a log could show."""
    raw = target.model_dump_json(by_alias=True, exclude_none=True)
    if "{{" not in raw:
        return target

    def fill(match) -> str:
        if match.group(1) not in inputs:
            raise TemplateError(f"{{{{inputs.{match.group(1)}}}}} has no value")
        return json.dumps(str(inputs[match.group(1)]))[1:-1]  # escaped for the JSON it's spliced into

    return type(target).model_validate(json.loads(_INPUT.sub(fill, raw)))


def render(text: str, inputs: dict[str, str], env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env

    def fill(match) -> str:
        scope, name = match.group(1), match.group(2)
        source = inputs if scope == "inputs" else env
        if name not in source:
            where = f" (looked up as {source.key(name)})" if isinstance(source, TenantSecrets) else ""
            raise TemplateError(f"{{{{{scope}.{name}}}}} has no value{where}")
        return str(source[name])

    return TEMPLATE.sub(fill, text)
