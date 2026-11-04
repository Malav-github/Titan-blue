"""Readable text for structured API responses, so a claim can quote "Authors in order: 1. Ashish Vaswani; 2. ..." and not a
page of nested JSON. Plain code over known response shapes: nothing is summarised, guessed or reordered, every value is copied
from a field, and the raw response stays archived and hashed beside it. An unknown shape returns None and the raw JSON is used.
"""

import json
from urllib.parse import urlparse


def _authors(work: dict) -> list[str]:
    out = []
    for i, a in enumerate(work.get("authorships") or [], 1):
        name = ((a or {}).get("author") or {}).get("display_name")
        if not name:
            continue
        places = [x.get("display_name") for x in (a.get("institutions") or []) if x.get("display_name")]
        out.append(f"{i}. {name}" + (f" ({'; '.join(places[:2])})" if places else ""))
    return out


def _work(work: dict, n: int | None = None) -> str:
    head = f"Work {n}: " if n else "Work: "
    lines = [f"{head}{work.get('display_name') or work.get('title') or '(untitled)'}"]
    for label, key in (("Publication year", "publication_year"), ("Publication date", "publication_date"), ("Type", "type"),
                       ("Cited by", "cited_by_count"), ("DOI", "doi"), ("OpenAlex id", "id")):
        if work.get(key) not in (None, ""):
            lines.append(f"{label}: {work[key]}")
    venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    if venue:
        lines.append(f"Venue: {venue}")
    if authors := _authors(work):
        lines.append(f"Authors in order ({len(authors)}): " + "; ".join(authors))
    return "\n".join(lines)


def _author(item: dict, n: int) -> str:
    lines = [f"Author {n}: {item.get('display_name')}"]
    if item.get("works_count") is not None:
        lines.append(f"Works: {item['works_count']}")
    if item.get("cited_by_count") is not None:
        lines.append(f"Cited by: {item['cited_by_count']}")
    places = [x.get("display_name") for x in (item.get("last_known_institutions") or []) if x.get("display_name")]
    if places:
        lines.append("Last known institutions: " + "; ".join(places))
    return "\n".join(lines)


def _openalex(data) -> str | None:
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items = data["results"]
        if not items:
            return None
        if all("authorships" in x for x in items if isinstance(x, dict)):
            return "OpenAlex works, best match first.\n\n" + "\n\n".join(_work(w, i) for i, w in enumerate(items[:10], 1))
        if all("works_count" in x for x in items if isinstance(x, dict)):
            return "OpenAlex authors, best match first.\n\n" + "\n\n".join(_author(a, i) for i, a in enumerate(items[:10], 1))
        return None
    if isinstance(data, dict) and "authorships" in data and data.get("display_name"):
        return "OpenAlex work.\n\n" + _work(data)
    if isinstance(data, dict) and "works_count" in data and data.get("display_name"):
        return "OpenAlex author.\n\n" + _author(data, 1)
    return None


def render_json(url: str, text: str) -> str | None:
    """Readable text for a response from a known API, or None."""
    if (urlparse(url).hostname or "") != "api.openalex.org":
        return None
    try:
        return _openalex(json.loads(text))
    except (ValueError, AttributeError, TypeError):
        return None
