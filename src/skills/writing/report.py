"""Writing a report (report_types.json): section by section, then a summary, then appendices built by code.

Paraphrase is allowed here (the user's rule: the exact wording is checked by verification, against the verbatim quotes). What
is not allowed is a fact the claims do not carry, so code locks every sentence after it is written:
  - a sentence that states something must cite at least one claim, and only claims given to that section;
  - every number in it must be in the claims it cites (the quotes, their values, their dates).
A sentence that fails is removed, and the report says how many were. Sections are written one call each (the local model
has an 8,192-token context and writes about 43 tokens a second), each from only its own claims; a section with no claims says
so, and when time runs out a section falls back to its quotes rather than being skipped.

    write(ask, title, sections, rows_of, scope, today, time_left) -> (markdown, info)
"""

import re
from datetime import date

from skills.writing import evaluate
from skills.writing.facts import build_facts
from skills.writing.prompt import draft_prompt

CITE = re.compile(r"\[claim:(\d+)\]")
SECTION_SECONDS = 25          # one section's call, written in full (about 300 words), with room to spare
SUMMARY_SECONDS = 20


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“(])|\n+", text.strip()) if s.strip()]


def lock(text: str, facts: list) -> tuple[str, list[str]]:
    """Keep only the sentences that rest on the claims they cite: each cites a claim given to this text, and every number in it
    is in those claims. Returns (kept text, removed sentences)."""
    by_id = {f.claim_id: f for f in facts}
    kept, removed = [], []
    for s in _sentences(text):
        ids = [int(i) for i in CITE.findall(s)]
        cited = [by_id[i] for i in ids if i in by_id]
        plain = CITE.sub("", s).strip()
        if not plain:
            continue
        states = bool(re.search(r"[A-Za-z]{3,}", plain))
        if states and not cited:
            removed.append(s)                             # says something, rests on nothing given here
            continue
        allowed = set()
        for f in cited:
            allowed |= evaluate._numbers(f"{f.text} {f.value} {f.quote} {f.iso() if f.date else ''}")
            if f.date:
                allowed |= {str(f.date.year), str(f.date.day), str(f.date.month)}
        if [n for n in evaluate._numbers(plain) if n not in allowed and n.lstrip("0") not in allowed]:
            removed.append(s)                             # a figure no cited claim carries
            continue
        kept.append(s)
    return " ".join(kept), removed


_SEQUENCE = re.compile(r"^(Earlier|Later|Previously|Subsequently|Afterwards|Then),\s+", re.I)


def fix_sequence(text: str, facts: list) -> tuple[str, int]:
    """A small model writes 'Earlier, on 28 October ...' after an event of 22 October. A sequencing word that the cited claims'
    dates contradict is removed (the date in the sentence still says when). Returns (text, how many were removed)."""
    by_id = {f.claim_id: f for f in facts}
    out, last, fixed = [], None, 0
    for sent in _sentences(text):
        dates = [by_id[int(i)].date for i in CITE.findall(sent) if int(i) in by_id and by_id[int(i)].date]
        when = max(dates) if dates else None
        m = _SEQUENCE.match(sent)
        if m and when and last:
            earlier = m.group(1).lower() in ("earlier", "previously")
            if (earlier and when > last) or (not earlier and when < last):
                sent = sent[m.end():]
                sent = sent[:1].upper() + sent[1:]
                fixed += 1
        out.append(sent)
        last = when or last
    return " ".join(out), fixed


def _section_form(words: tuple[int, int]) -> dict:
    lo, hi = words
    return {"id": "report_section", "label": "Report section", "words": [lo, hi],
            "purpose": "One section of a research report: answer the section's question from its claims.",
            "reader": "A reader who needs the facts, their dates and their sources, plainly put.",
            "structure": ["The section's answer in the first sentence.", "Then each supporting claim, dated, with its source named.",
                          "Last, what the claims do not establish for this question, if anything."],
            "rules": ["Paraphrase freely, but every fact, figure, date and name comes from the claims given.",
                      "Say how strong the evidence is where it is thin (one source, a report rather than the company's own document)."]}


def write(ask, title: str, sections: list[str], rows_of, scope: dict | None, today: date, time_left, words=(150, 300),
          quote_line=None) -> tuple[str, dict]:
    """`ask(prompt) -> text | None` calls the model; `rows_of(i)` gives section i's claim rows; `time_left()` the seconds left;
    `quote_line(row)` how a quote is cited when a section falls back to its quotes."""
    parts, removed, fallback, empty = [], [], [], []
    sequence = 0
    written: list[tuple[str, str]] = []
    form = _section_form(words)
    for i, heading in enumerate(sections):
        rows = rows_of(i)
        if not rows:
            empty.append(heading)
            parts.append(f"## {heading}\n\nNot established from what was read.")
            continue
        facts = build_facts(rows)
        text = None
        if time_left() > SECTION_SECONDS + SUMMARY_SECONDS:
            draft = ask(draft_prompt(heading, facts, form, scope, today, []))
            if draft:
                text, gone = lock(draft, facts)
                removed += gone
                text, n = fix_sequence(text, facts)
                sequence += n
        if not text:                                     # no time, or nothing survived the lock: the section's own quotes
            fallback.append(heading)
            text = "\n".join(f"- {quote_line(r)}" for r in rows[:6]) if quote_line else "Not written in time."
        written.append((heading, text))
        parts.append(f"## {heading}\n\n{text}")
    summary = ""
    if written and time_left() > SUMMARY_SECONDS:
        all_facts = build_facts([r for i in range(len(sections)) for r in rows_of(i)])
        digest = "\n\n".join(f"{h}\n{t}" for h, t in written)
        draft = ask(f"TODAY: {today.isoformat()}\nREPORT: {title}\n\nSECTIONS (already written, each sentence with its claim tags):\n"
                    f"{digest}\n\nWrite the report's summary: 80 to 150 words that stand alone, the main findings first, each sentence "
                    f"ending with the tags [claim:N] of the claims it rests on, copied from the sections. Add nothing the sections do not say.")
        if draft:
            summary, gone = lock(draft, all_facts)
            removed += gone
    body = (f"## Summary\n\n{summary}\n\n" if summary else "") + "\n\n".join(parts)
    return body, {"sections": len(sections), "sections_empty": empty, "sections_from_quotes": fallback,
                  "sentences_removed": removed, "summary": bool(summary), "sequence_words_removed": sequence}
