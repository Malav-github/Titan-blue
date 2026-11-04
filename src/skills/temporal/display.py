"""How dates are shown to a person or to a prompt."""

import re

_ISO = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")


def short_date(value: str | None) -> str | None:
    """'2024-05-03T10:11:12' -> '2024-05-03'; '2024-05' stays; anything else (for example '3 days ago') is kept, trimmed."""
    if not value:
        return None
    text = str(value).strip()
    m = _ISO.match(text)
    if m:
        return "-".join(g for g in m.groups() if g)
    return text[:24] or None
