"""Templates: ready-made apps stored as data (definitions/templates/*.json). The UI draws the form from `inputs`."""

import json
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from config import PROJECT_ROOT

TEMPLATES_DIR = PROJECT_ROOT / "definitions" / "templates"


class Template(BaseModel):
    id: str
    version: int
    title: str
    summary: str
    kind: Literal["research", "capability"]
    inputs: dict
    result: list[str]
    order: int = 100            # position on the Home screen, lowest first
    owner: str = "system"
    permissions: dict = {}

    def check(self) -> "Template":
        Draft202012Validator.check_schema(self.inputs)
        return self

    def validate_inputs(self, values: dict) -> tuple[dict, list[str]]:
        """Defaults filled in; a list of everything wrong, so a form can show all of it at once."""
        props = self.inputs.get("properties", {})
        merged = {k: p["default"] for k, p in props.items() if "default" in p} | {k: v for k, v in values.items() if k in props}
        problems = [f"unknown field {k!r}" for k in values if k not in props]
        problems += [f"{'.'.join(str(x) for x in e.path) or 'form'}: {e.message}" for e in Draft202012Validator(self.inputs).iter_errors(merged)]
        return merged, problems


def load_templates(directory: Path = TEMPLATES_DIR) -> dict[str, Template]:
    out = {}
    for f in sorted(directory.glob("*.json")):
        t = Template(**json.loads(f.read_text(encoding="utf-8"))).check()
        if t.id in out:
            raise ValueError(f"duplicate template {t.id}")
        out[t.id] = t
    return dict(sorted(out.items(), key=lambda kv: (kv[1].order, kv[0])))
