"""Extraction by sentence choice: the model is shown a passage as numbered sentences and answers with numbers; code copies
the sentences. A claim is then the source's own words, never a paraphrase, and its quote cannot be misquoted: there is
nothing for the model to retype. The model still decides which sentences state a needed fact.

    split(text) -> [sentence]          the units shown to the model, in order
    numbered(sentences) -> str         "[1] ...\n[2] ..."
    pick(sentences, numbers) -> (quote | None, why)
"""

import re

MAX_SENTENCES = 3          # one fact rests on at most this many adjacent sentences
MAX_CHARS = 700            # a longer choice is a passage, not a statement of a fact
MAX_UNIT = 500             # a longer line (a table flattened to text) is cut at clause marks so its parts can be chosen

_NEXT = r"(?=[\"“(]?[A-Z0-9₹$₱€£])"
_END = re.compile(rf"(?<=[.!?])\s+{_NEXT}|(?<=[.!?][\"”’)])\s+{_NEXT}")      # only the space between is dropped
# a full stop after these does not end a sentence ("Acme Inc. said", "the U.S. market", "Rs. 4,767 crore")
_NO_END = re.compile(r"(?:\b(?:Inc|Corp|Co|Ltd|Pvt|Plc|LLC|No|Nos|Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|approx|Rs|Fig|Sec|Art|Vol|Ref|Jan|Feb|"
                     r"Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)|\b[A-Z]|\b(?:U\.S|U\.K|e\.g|i\.e|a\.m|p\.m))\.$")


def split(text: str) -> list[str]:
    """Sentences, in order. Lines are kept apart (a heading, a table row), sentences within a line are split, and a very
    long line is cut at semicolons or bullets so its parts can be chosen. Whitespace is collapsed; words are not touched."""
    out: list[str] = []
    for line in re.split(r"\n+", text):
        line = " ".join(line.split())
        if not line:
            continue
        parts = _END.split(line)
        merged: list[str] = []
        for p in parts:
            if merged and _NO_END.search(merged[-1]):
                merged[-1] = f"{merged[-1]} {p}"
            else:
                merged.append(p)
        for s in merged:
            out += _cut(s) if len(s) > MAX_UNIT else [s]
    return [s for s in out if s.strip()]


# where a long line may be cut, gentlest first: clauses and bullets; the cells of a table flattened to text; the fields of
# page metadata ("DC.title: ...", "citation_pdf_url: ...")
_CUTS = [re.compile(r"(?<=;)\s+|\s+(?=[•▪●]\s)"), re.compile(r"\s+(?=\|\s)"),
         re.compile(r"\s+(?=(?:[A-Z][A-Za-z]*\.)?[a-z][a-z_]*[A-Za-z_.]*(?:\s\([^)]{1,30}\))?:\s)|\s+(?=[A-Z]{2,}\.[a-z]+:\s)")]


PACK = MAX_UNIT // 2      # the parts of a cut line are packed back together up to this, so a table cell keeps its row


def _cut(s: str) -> list[str]:
    """A long line cut into parts that can each be chosen; a part still too long is cut by the next kind of mark. The
    pieces are packed back together, in order, up to PACK characters: a lone cell ('| US20210061972A1 (en)') says
    nothing, the cells around it say what it is."""
    parts = [s]
    for regex in _CUTS:
        if all(len(p) <= MAX_UNIT for p in parts):
            break
        parts = [q for p in parts for q in (_pack([x.strip() for x in regex.split(p) if x.strip()]) if len(p) > MAX_UNIT else [p])]
    return parts


def _pack(atoms: list[str]) -> list[str]:
    out: list[str] = []
    for a in atoms:
        if out and len(out[-1]) + 1 + len(a) <= PACK:
            out[-1] = f"{out[-1]} {a}"
        else:
            out.append(a)
    return out


def windows(units: list[str], size: int, overlap: int = 2) -> list[list[str]]:
    """Runs of whole sentences of about `size` characters, each starting `overlap` sentences before the last one ended:
    a fact is never cut in half at a window's edge."""
    out, i = [], 0
    while i < len(units):
        j, n = i, 0
        while j < len(units) and (j == i or n + len(units[j]) <= size):
            n += len(units[j]) + 1
            j += 1
        out.append(units[i:j])
        if j >= len(units):
            break
        i = max(i + 1, j - overlap)
    return out


def numbered(sentences: list[str]) -> str:
    return "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences, 1))


MAX_PIECES = 12           # quotes one choice may give (the members of a patent family, each in its own table row)


def pick(sentences: list[str], numbers: list[int]) -> tuple[str | None, str]:
    """The quote for a choice of adjacent sentences (at most MAX_SENTENCES), copied from the passage, or None and why."""
    nums = sorted(set(numbers or []))
    if not nums:
        return None, "no sentence chosen"
    if bad := [n for n in nums if not 1 <= n <= len(sentences)]:
        return None, f"no sentence numbered {', '.join(map(str, bad))} in the passage"
    if len(nums) > MAX_SENTENCES:
        return None, f"more than {MAX_SENTENCES} sentences chosen for one fact"
    if nums != list(range(nums[0], nums[-1] + 1)):
        return None, "the chosen sentences are not next to each other"
    quote = " ".join(sentences[n - 1] for n in nums)
    if len(quote) > MAX_CHARS:
        return None, f"the chosen sentences run to more than {MAX_CHARS} characters"
    return quote, ""


def pick_all(sentences: list[str], numbers: list[int]) -> list[tuple[str | None, str]]:
    """Every quote a choice gives. A fact spread over a page (a list in separate table rows) comes back as several numbers
    that are not next to each other: each run of adjacent sentences becomes its own quote, a long run is cut into runs of
    MAX_SENTENCES, so every claim is still a sentence or two of the source, word for word. Numbers the passage does not
    have are refused, said once."""
    nums = sorted(set(numbers or []))
    if not nums:
        return [(None, "no sentence chosen")]
    out: list[tuple[str | None, str]] = []
    if bad := [n for n in nums if not 1 <= n <= len(sentences)]:
        out.append((None, f"no sentence numbered {', '.join(map(str, bad))} in the passage"))
        nums = [n for n in nums if n not in bad]
    runs: list[list[int]] = []
    for n in nums:
        if runs and n == runs[-1][-1] + 1 and len(runs[-1]) < MAX_SENTENCES:
            runs[-1].append(n)
        else:
            runs.append([n])
    for run in runs[:MAX_PIECES]:
        out.append(pick(sentences, run))
    if len(runs) > MAX_PIECES:
        out.append((None, f"more than {MAX_PIECES} separate passages chosen for one fact; the rest were not read"))
    return out
