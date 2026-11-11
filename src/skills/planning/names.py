"""Names of things in what was read (definitions/guidance/item_names.json), picked out by code.

    named_items(texts, exclude) -> ['MoReTec', 'CirculenRevive', 'APK', ...]   most mentioned first

What counts as a name: a word marked ™ or ®, a word with a capital inside it ('MoReTec', 'CirculenRevive'), or an all-capital
word of three to six letters ('APK', 'QCP'), never one of the stop-list's trade abbreviations, and never the subject's own name.
It finds candidates to search for and to group by; it never decides that something is true.
"""

import json
import re
from collections import Counter
from functools import lru_cache

from config import PROJECT_ROOT

PATH = PROJECT_ROOT / "definitions" / "guidance" / "item_names.json"

_MARKED = re.compile(r"\b([A-Z][\w-]{1,30}(?:\s[A-Z][\w-]{1,30})?)\s?(?:™|®|\(TM\)|\(R\))")
_INNER = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+|[A-Z]{1,3}[a-z]*)+)\b")        # MoReTec, CirculenRevive, LyondellBasell
_CAPS = re.compile(r"\b([A-Z]{3,6})\b")


@lru_cache(maxsize=1)
def config() -> dict:
    d = json.loads(PATH.read_text(encoding="utf-8"))
    d["_stop"] = {w.lower() for w in d["not_names"]}
    return d


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def named_items(texts: list[str], exclude: list[str] | None = None) -> list[str]:
    """Names in `texts`, most mentioned first, without the stop-list and without anything in `exclude` (the subject's names)."""
    cfg = config()
    banned = {_squash(e) for e in exclude or [] if e} | {_squash(w) for e in exclude or [] if e for w in e.split()}
    counts: Counter = Counter()
    first: dict[str, str] = {}
    for text in texts:
        found = [m.group(1) for m in _MARKED.finditer(text)] + [m.group(1) for m in _INNER.finditer(text)]
        # an all-capital word only counts when the text is not shouting: headings in capitals are not names
        if sum(1 for _ in _CAPS.finditer(text)) * 8 < len(text.split()) + 8:
            found += [m.group(1) for m in _CAPS.finditer(text)]
        for name in found:
            key = _squash(name)
            if len(key) < 3 or name.lower() in cfg["_stop"] or key in banned or any(b and b in key for b in banned if len(b) >= 5):
                continue
            counts[key] += 1
            first.setdefault(key, name)
    ranked = [first[k] for k, n in counts.most_common() if n >= cfg["min_mentions"]]
    return ranked[:cfg["max_names"]]


def mentions(text: str, name: str) -> bool:
    """Does `text` name `name` (as a word, any case)?"""
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I))
