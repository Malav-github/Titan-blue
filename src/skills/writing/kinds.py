"""Which kind of text a request asks for, decided by plain rules (the same request always gets the same form)."""

import re

from skills.writing import forms

# More specific forms are tried first; a request that names none of them is a news brief when it asks for the latest, else a short answer.
_ORDER = ("white_paper", "essay", "long_form", "report", "research_note", "short_form", "news")


def form_by_id(form_id: str) -> dict:
    return next(f for f in forms() if f["id"] == form_id)


def choose_form(question: str, scope: dict | None = None) -> dict:
    text = (question or "").lower()
    for form_id in _ORDER:
        f = form_by_id(form_id)
        if f.get("route_when") and re.search(f["route_when"], text):
            return f
    if scope and scope.get("intent") == "latest":
        return form_by_id("news")
    return form_by_id("answer")


def written_as(form: dict) -> dict:
    """The form the runner can write today. A form it does not generate yet is written as the nearest one that it does."""
    if form.get("generated"):
        return form
    return form_by_id("news" if form["id"] in ("short_form", "research_note", "report") else "answer")
