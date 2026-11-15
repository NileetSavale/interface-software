"""Filling {{inputs.x}} and {{secrets.X}} at act time. Values never go back into the artifact or the logs."""
import os

from interfaze.artifact.schema import TEMPLATE


class TemplateError(ValueError):
    pass


def render(text: str, inputs: dict[str, str], env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env

    def fill(match) -> str:
        scope, name = match.group(1), match.group(2)
        source = inputs if scope == "inputs" else env
        if name not in source:
            raise TemplateError(f"{{{{{scope}.{name}}}}} has no value")
        return str(source[name])

    return TEMPLATE.sub(fill, text)
