"""The writing skill: how Titan writes what it found.

Three parts, all driven by data in definitions/ so that tuning them is an edit and not a code change:
  * forms and rules (writing_forms.json, writing_rules.json): which kind of text fits the request and what every text must do,
    with materiality, relevance and time first;
  * facts (facts.py): the established claims with the date each event happened (or the plain statement that it has none), grouped
    into moments, so the writer cannot treat events years apart as one story;
  * an evaluation matrix (writing_matrix.json, evaluate.py): scores a written output by code on the same dimensions a person
    scores it on, and golden.py keeps the person's scores so the two can be compared and the code's checks tuned.

It never searches, reads a page or judges evidence. It only decides how the established claims are put into words.
"""

import json
import re
from functools import lru_cache

from config import PROJECT_ROOT

MANIFEST_PATH = PROJECT_ROOT / "definitions" / "skills" / "writing.json"
RULES_PATH = PROJECT_ROOT / "definitions" / "guidance" / "writing_rules.json"
FORMS_PATH = PROJECT_ROOT / "definitions" / "guidance" / "writing_forms.json"
MATRIX_PATH = PROJECT_ROOT / "definitions" / "evaluation" / "writing_matrix.json"


@lru_cache(maxsize=1)
def manifest() -> dict:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for key in ("name", "version", "settings"):
        if key not in data:
            raise ValueError(f"{MANIFEST_PATH.name}: {key} is missing")
    return data


def setting(name: str):
    return manifest()["settings"][name]


@lru_cache(maxsize=1)
def rules() -> list[dict]:
    """The rule areas, each with its rules."""
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    for area in data["areas"]:
        for key in ("id", "label", "why", "rules"):
            if key not in area:
                raise ValueError(f"writing_rules.json: {area.get('id', '?')} is missing {key}")
        for r in area["rules"]:
            if not r.get("check", "").startswith(("auto:", "review")):
                raise ValueError(f"writing_rules.json: {r.get('id', '?')}: check must be auto:<name> or review")
    return data["areas"]


@lru_cache(maxsize=1)
def forms() -> list[dict]:
    data = json.loads(FORMS_PATH.read_text(encoding="utf-8"))
    for f in data["forms"]:
        for key in ("id", "label", "purpose", "reader", "words", "structure", "rules"):
            if key not in f:
                raise ValueError(f"writing_forms.json: {f.get('id', '?')} is missing {key}")
        if f.get("route_when"):
            re.compile(f["route_when"])
        if not (len(f["words"]) == 2 and 0 < f["words"][0] < f["words"][1]):
            raise ValueError(f"writing_forms.json: {f['id']}: words must be [min, max]")
    return data["forms"]


@lru_cache(maxsize=1)
def matrix() -> dict:
    data = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    if abs(sum(d["weight"] for d in data["dimensions"]) - 100) > 1e-9:
        raise ValueError("writing_matrix.json: the weights must add to 100")
    return data


from skills.writing.facts import Fact, build_facts, groups  # noqa: E402,F401
from skills.writing.kinds import choose_form, form_by_id, written_as  # noqa: E402,F401
from skills.writing.prompt import draft_prompt, revision_prompt  # noqa: E402,F401
from skills.writing.evaluate import drop_uncited, score  # noqa: E402,F401
