"""The temporal skill: Titan knows what time it is, what time frame a question is about, and what dates its sources carry.

Dates come from code first and models second (see docs in definitions/skills/temporal.json and the plan that created
this package). Phase 1 here: the clock, the question's time scope, and search that uses both.
"""

import json
from functools import lru_cache

from config import PROJECT_ROOT

MANIFEST_PATH = PROJECT_ROOT / "definitions" / "skills" / "temporal.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    """The skill's name, version and tunable settings (thresholds live in data, not in code)."""
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for key in ("name", "version", "settings"):
        if key not in data:
            raise ValueError(f"{MANIFEST_PATH.name}: {key} is missing")
    return data


def setting(name: str):
    return manifest()["settings"][name]
