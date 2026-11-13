"""The roles skill: who holds a role at a company, and whether a statement about it is right, by code.

    parse(statement)            -> RoleStatement | None   'NVIDIA CEO Jensen Hu-ang resigns over governance failure'
                                   -> org NVIDIA, role CEO, person 'Jensen Hu-ang', event departure, reason 'governance failure'
    same_person(written, named) -> {match: same | spelling | different, why}
    certifier(text)             -> {name, title} from a Sarbanes-Oxley certification (Exhibit 31.1: the principal executive
                                   officer; 31.2: the principal financial officer), which every 10-Q and 10-K carries
    event_sentence(text, surname, event) -> the filing's own sentence reporting a departure or an appointment

A company has one chief executive at a time, so the person a statement names is compared with the one the company's own filing
names, by surname first: 'Hu-ang' is 'Huang' misspelled, and 'Jensen' and 'Jen-Hsun' are the same first name as people use it
and as the filing gives it. A US-listed company must report a chief executive's departure or appointment on Form 8-K, Item 5.02,
within four business days, which makes the absence of one meaningful, said with that window.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass

ROLES = {
    "ceo": r"CEO|chief executive(?: officer)?",
    "cfo": r"CFO|chief financial officer",
    "coo": r"COO|chief operating officer",
    "cto": r"CTO|chief technology officer",
    "chair": r"chair(?:man|woman|person)?(?: of the board)?",
    "president": r"president",
}
CERTIFYING = {"ceo": "EX-31.1", "cfo": "EX-31.2"}       # the officer who signs each certification
LABELS = {"ceo": "chief executive officer", "cfo": "chief financial officer", "coo": "chief operating officer",
          "cto": "chief technology officer", "chair": "chair", "president": "president"}
# Role words match in any case; names do not: a name is a run of capitalised words, so it stops at 'resigns' or 'said'.
_ANYROLE = "(?i:" + "|".join(ROLES.values()) + ")"
_ROLE = rf"(?P<roles>(?i:co-)?{_ANYROLE}(?:(?:,\s*|\s+and\s+){_ANYROLE})*)"
_NAME_WORD = r"[A-Z][a-zA-Z'’.\-]+"
_PERSON = rf"{_NAME_WORD}(?:\s+(?:[A-Z]\.\s+)?{_NAME_WORD}){{1,3}}"
_ORG = r"[A-Z][\w&.'’\-]*(?:\s+(?:[A-Z][\w&.'’\-]*|&|of|and)(?=\s|$|[,.;:]))*"
DEPARTURE = r"resign\w*|step(?:s|ped|ping)? down|quit\w*|fired|ousted|retir\w*|depart\w*|leav(?:es|ing)|left|exit\w*|terminat\w*|removed|replaced"
APPOINTMENT = r"appoint\w*|nam(?:ed|es)|hired?|join\w*|promot\w*|succeed\w*|becomes?|became|takes? over|elected"
_REASON = re.compile(r"\b(?:over|because of|due to|amid|after|following|citing)\s+(?P<r>[^.;,]{4,80})", re.I)

_PATTERNS = [
    # 'NVIDIA CEO Jensen Huang resigns', 'NVIDIA's CEO, Jensen Huang, ...'
    re.compile(rf"(?P<org>{_ORG}?)(?:['’]s)?\s+{_ROLE},?\s+(?P<person>{_PERSON})"),
    # 'Jensen Huang, CEO of NVIDIA', 'Jensen Huang, the chief executive of NVIDIA'
    re.compile(rf"(?P<person>{_PERSON}),\s+(?i:the\s+)?{_ROLE}\s+(?i:of|at)\s+(?P<org>{_ORG})"),
    # 'Jensen Huang is the CEO of NVIDIA', 'Martina Cheung is the President and Chief Executive Officer of S&P Global'
    re.compile(rf"(?P<person>{_PERSON})\s+(?i:is|was|became|has been|will be|remains|serves as|served as)\s+(?i:the\s+)?(?i:new\s+|interim\s+)?"
               rf"{_ROLE}\s+(?i:of|at)\s+(?P<org>{_ORG})"),
    # 'NVIDIA named Colette Kress (as) interim CEO'
    re.compile(rf"(?P<org>{_ORG})\s+(?i:has\s+)?(?i:named|appointed|appoints|names|hired|hires|elected|elects)\s+(?P<person>{_PERSON})\s+"
               rf"(?i:as\s+)?(?i:its\s+)?(?i:new\s+|interim\s+)?{_ROLE}"),
]
_PRIORITY = ["ceo", "cfo", "coo", "cto", "chair", "president"]     # 'President and Chief Executive Officer' is the CEO
_NOT_ORG = {"the", "a", "an", "its", "their", "his", "her", "former", "new", "interim", "then"}


@dataclass
class RoleStatement:
    org: str
    role: str                  # ceo | cfo | coo | cto | chair | president
    person: str
    event: str                 # departure | appointment | holds
    reason: str | None = None

    def as_dict(self) -> dict:
        return {"org": self.org, "role": self.role, "role_label": LABELS[self.role], "person": self.person, "event": self.event,
                "reason": self.reason}


def _clean_org(org: str) -> str:
    words = org.split()
    while words and words[0].lower() in _NOT_ORG:
        words = words[1:]
    return re.sub(r"['’]s$", "", " ".join(words)).strip(" ,'’").removesuffix(".") if not re.search(r"\b(?:Inc|Corp|Co|Ltd)\.$", org) else " ".join(words)


def parse(statement: str) -> RoleStatement | None:
    text = " ".join(statement.split())
    for pat in _PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        role = next(k for k in _PRIORITY if re.search(rf"\b(?:{ROLES[k]})\b", m["roles"], re.I))
        org, person = _clean_org(m["org"] or ""), m["person"].strip(" ,")
        person = re.sub(rf"\s+(?:{DEPARTURE}|{APPOINTMENT})$", "", person, flags=re.I)   # 'Jensen Huang Resigns' in a headline
        if not org or not person or len(person.split()) < 2:
            continue
        rest = text[m.end():] + " " + text[:m.start()]
        event = ("departure" if re.search(rf"\b(?:{DEPARTURE})\b", rest, re.I) else
                 "appointment" if re.search(rf"\b(?:{APPOINTMENT})\b", text, re.I) and pat is not _PATTERNS[2] else "holds")
        reason = _REASON.search(text[m.end():])
        return RoleStatement(org, role, person, event, reason["r"].strip() if reason else None)
    return None


def _letters(word: str) -> str:
    word = unicodedata.normalize("NFKD", word)
    return re.sub(r"[^a-z]", "", word.encode("ascii", "ignore").decode().lower())


def _parts(name: str) -> tuple[str, str]:
    words = [w for w in re.split(r"\s+", name.strip()) if w and not re.fullmatch(r"[A-Z]\.?|Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Jr\.?|Sr\.?|III?|IV", w)]
    return (words[0], words[-1]) if words else ("", "")


def same_person(written: str, named: str) -> dict:
    """Is the person a statement names the one a filing names? Surname first (a company has one chief executive):
    'Jensen Hu-ang' and 'Jen-Hsun Huang' are the same surname spelled differently, and first names that begin alike
    ('Jensen', 'Jen-Hsun') are how a person is called and how the filing gives the name."""
    wf, wl = _parts(written)
    nf, nl = _parts(named)
    a, b = _letters(wl), _letters(nl)
    if not a or not b:
        return {"match": "different", "why": "no surname to compare"}
    surname = 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()
    fa, fb = _letters(wf), _letters(nf)
    first_alike = fa == fb or (fa[:3] == fb[:3] and len(fa) >= 3) or fa[:1] == fb[:1] and (len(fa) <= 2 or len(fb) <= 2)
    if surname < 0.8 or (surname < 1.0 and not first_alike):
        return {"match": "different", "why": f"'{written}' is not '{named}'"}
    if wl == nl and wf == nf:
        return {"match": "same", "why": f"'{written}' as the filing names"}
    notes = []
    if wl.lower() != nl.lower():
        notes.append(f"the surname is written '{wl}' where the filing writes '{nl}'")
    if wf.lower() != nf.lower():
        notes.append(f"the first name is '{wf}' where the filing gives '{nf}' (the name used and the name filed can differ)")
    # a surname written otherwise ('Hu-ang', 'Haung') is a misspelling; a first name used otherwise ('Jensen' for the filed
    # 'Jen-Hsun') is not
    return {"match": "spelling" if wl.lower() != nl.lower() else "same",
            "why": "; ".join(notes) or f"'{written}' as the filing names"}


def certifier(text: str) -> dict | None:
    """The officer who signed a Sarbanes-Oxley certification: 'I, Jen-Hsun Huang, certify that' and the title under the
    signature ('President and Chief Executive Officer')."""
    m = re.search(r"\bI,\s+(?P<name>[A-Z][^,\n]{2,60}?),\s+certify", text)
    if not m:
        return None
    title = re.search(r"\n\s*(?P<t>(?:(?:[A-Z][\w,&]*|and)\s+){0,6}(?:Chief|Principal) (?:Executive|Financial)[^\n]{0,60})", text[m.end():])
    return {"name": " ".join(m["name"].split()), "title": " ".join(title["t"].split()) if title else "", "quote": m[0]}


def surname_of(name: str) -> str:
    return _parts(name)[1]


# how near the event word must be to the person's name: 'accepted the resignation of Jen-Hsun Huang' is Huang's departure;
# 'Following Mr. Huang's resignation, the Board appointed ... Colette M. Kress' is not Kress's
NEAR = {"departure": 60, "appointment": 130}


def event_sentence(text: str, surnames: list[str], event: str) -> str | None:
    """The filing's own sentence that reports the event for the person (by surname, the event word near the name): word
    for word, or None."""
    words = re.compile(rf"\b(?:{DEPARTURE if event == 'departure' else APPOINTMENT})\b", re.I)
    keys = {_letters(s) for s in surnames if s}
    import sentences                              # the project's splitter: 'Colette M. Kress' is not two sentences
    for sent in sentences.split(text):
        names = [m.start() for m in re.finditer(r"[A-Za-z][\w’'\-]*", sent)
                 if any(k and (_letters(m[0]) == k or _letters(m[0]) == k + "s") for k in keys)]     # 'Huang', 'Huang’s'
        if names and any(min(abs(w.start() - n) for n in names) <= NEAR[event] for w in words.finditer(sent)):
            return sent
    return None


def says_reason(text: str, reason: str) -> bool:
    """Does the filing give the stated reason? Its content words (not 'the', 'over'), all of them, near each other or not."""
    words = [w for w in re.findall(r"[a-z]{4,}", reason.lower()) if w not in {"over", "after", "amid", "following", "their", "with"}]
    low = text.lower()
    return bool(words) and all(w[:6] in low for w in words)
