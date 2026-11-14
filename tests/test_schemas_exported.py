"""The committed JSON Schemas must match the code, so reviewers never read a stale contract."""
import json

from scripts.export_schemas import OUT, SCHEMAS


def test_committed_schemas_are_current():
    for name, schema in SCHEMAS.items():
        assert json.loads((OUT / name).read_text()) == schema, f"schemas/{name} is stale: run scripts/export_schemas.py"
