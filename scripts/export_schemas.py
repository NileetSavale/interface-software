"""Write the artifact and replay-result contracts as JSON Schema, for reviewers and non-Python callers.

    uv run python scripts/export_schemas.py
"""
import json
from pathlib import Path

from pydantic import TypeAdapter

from interface.artifact.schema import Artifact
from interface.replay.result import ReplayResult

OUT = Path(__file__).parent.parent / "schemas"

SCHEMAS = {
    "artifact.schema.json": Artifact.model_json_schema(by_alias=True),
    "replay-result.schema.json": TypeAdapter(ReplayResult).json_schema(by_alias=True),
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, schema in SCHEMAS.items():
        (OUT / name).write_text(json.dumps(schema, indent=2) + "\n")
        print(f"wrote schemas/{name}")
