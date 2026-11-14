"""Reading and writing artifacts. Stored artifacts are plain JSON; .jsonc (JSON with // comments) is accepted so the
annotated example in docs/ is validated by the same code."""
import json
from pathlib import Path

from interfaze.artifact.schema import Artifact


def strip_comments(text: str) -> str:
    """Drop // comments outside strings. "http://..." inside a string is kept."""
    out, in_string, escaped, i = [], False, False, 0
    while i < len(text):
        c = text[i]
        if in_string:
            out.append(c)
            escaped = c == "\\" and not escaped
            in_string = not (c == '"' and not escaped)
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def load_artifact(path: str | Path) -> Artifact:
    text = Path(path).read_text()
    if str(path).endswith(".jsonc"):
        text = strip_comments(text)
    return Artifact.model_validate(json.loads(text))


def dump_artifact(artifact: Artifact) -> str:
    return artifact.model_dump_json(by_alias=True, exclude_none=True, indent=2) + "\n"
