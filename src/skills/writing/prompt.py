"""The prompts that put established claims into words. They carry the dates, the moments and the form's rules, so the model does not
have to guess when anything happened."""

from datetime import date

from skills.writing import rules, setting
from skills.writing.facts import Fact, age_text, groups

_AREAS = ("time", "materiality", "relevance", "honesty")


def _listing(facts: list[Fact], today: date) -> str:
    lines = []
    for n, moment in enumerate(groups(facts), 1):
        if moment[0].dated:
            first, last = moment[-1].human(), moment[0].human()
            span = first if first == last else f"{first} to {last}"
            lines.append(f"MOMENT {n}: {span} ({age_text(moment[0], today)})")
        else:
            lines.append("NO RELIABLE DATE (keep these apart from the dated moments and say they are undated)")
        for f in moment:
            when = {"event": f"date {f.human()} ({f.origin})", "stated": f"date {f.human()} ({f.origin})",
                    "page": f"page dated {f.human()}, taken as the announcement date",
                    "reference": f.origin, "none": "no date found"}[f.basis]
            tags = "; ".join(x for x in (when, f.tier and f"{f.tier} source", f.corroboration) if x)
            lines.append(f"  [claim:{f.claim_id}] {f.text} ({tags})")
    return "\n".join(lines)


def _rules_text() -> str:
    out = []
    for area in rules():
        if area["id"] in _AREAS:
            out.append(f"{area['label'].upper()}: {area['why']}")
            out += [f"  - {r['rule']}" for r in area["rules"]]
    return "\n".join(out)


def _form_text(form: dict) -> str:
    lo, hi = form["words"]
    return (f"FORM: {form['label']}. {form['purpose']} Reader: {form['reader']} Length: {lo} to {hi} words.\n"
            "Build it like this:\n" + "\n".join(f"  {i}. {s}" for i, s in enumerate(form["structure"], 1)) + "\n"
            + "\n".join(f"  - {r}" for r in form["rules"]))


def draft_prompt(question: str, facts: list[Fact], form: dict, scope: dict | None, today: date, gaps: list[str]) -> str:
    latest = (scope or {}).get("intent") == "latest"
    example = next((f for f in facts if f.dated), facts[0] if facts else None)
    shape = (f"Shape of every sentence: '{'On ' + example.human() + ', ' if example.dated else ''}<who> <did what> [claim:{example.claim_id}].'"
             if example else "")
    return (
        f"TODAY: {today.isoformat()}\nQUESTION: {question}\n"
        f"{'The question asks for the LATEST: lead with the newest dated event and mark earlier ones as earlier.' + chr(10) if latest else ''}"
        f"\nVERIFIED CLAIMS, grouped by when they happened, newest first. Claims in different moments did NOT happen together.\n"
        f"{_listing(facts, today)}\n\n{_form_text(form)}\n\nRULES\n{_rules_text()}\n"
        + (f"\nNOT ESTABLISHED (say so in the answer): {'; '.join(gaps)}\n" if gaps else "")
        + f"\nWrite the answer using ONLY the claims above. {shape}\n"
          "A sentence that states a fact MUST end with the tag of the claim it rests on, written exactly like [claim:243]; a sentence "
          "without its tag is wrong. Write each date the way a person does ('27 October 2026'), never as 2026-10-27, and put it in the "
          "sentence that reports the event. Join events by time only ('earlier', 'later'). Do not say one caused, led to, followed or "
          "reinforced another unless a claim says so. Do not add anything that is not in the claims.")


def revision_prompt(first_prompt: str, draft: str, issues: list[str]) -> str:
    return (f"{first_prompt}\n\nYOUR EARLIER DRAFT:\n{draft}\n\nIT HAS THESE PROBLEMS:\n" + "\n".join(f"- {i}" for i in issues)
            + "\n\nWrite it again and fix every problem. Keep to the same claims and the same dates.")
