"""Score a written output by code, on the same six dimensions a person scores it on (definitions/evaluation/writing_matrix.json).

Every check returns a score from 0 to 1 and a plain sentence saying why, or None when it does not apply. A dimension is the mean of
its checks, the overall score is the weighted sum out of 100, and a blocker caps it. Nothing here calls a model: the same text
and the same facts always get the same score, so a change to the writer can be measured against a golden set.
"""

import re
from datetime import date

from skills.writing import matrix, setting
from skills.writing.facts import Fact, apart
from skills.writing.kinds import form_by_id

CITE = re.compile(r"\[claim:(\d+)\]")
_ABBREV = re.compile(r"\b(Inc|Corp|Ltd|Co|Mr|Mrs|Ms|Dr|St|vs|No|U\.S|U\.K|Jr|Sr|e\.g|i\.e|etc)\.", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_MONTH = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|may(?=\s+\d)"
_DATE_WORDS = re.compile(rf"\b(?:19|20)\d{{2}}\b|\b(?:{_MONTH})\b|\b(?:q[1-4]|fy ?\d{{2,4}}|h[12])\b", re.I)
_YEARS = re.compile(r"\b(?:19|20)\d{2}\b")
_STOP = set("the a an and or of in on at to for with by from as is are was were be been it its this that these those their his her they "
            "we our not no but if than then so also into over under after before about against between during while which who whom what "
            "when where how why all any each some such more most other can will would could should may might has have had do does did".split())
_CALENDAR = set("january february march april may june july august september october november december monday tuesday wednesday thursday "
                "friday saturday sunday".split())


def sentences(text: str) -> list[str]:
    """Sentences of a written output; abbreviations such as 'Inc.' do not end one."""
    protected = _ABBREV.sub(lambda m: m.group(0)[:-1] + "․", text.strip())
    parts = re.split(r"(?<=[.!?])(?:\s*\[claim:\d+\])*\s+(?=[A-Z\[\"'(])|\n+", protected)
    return [p.replace("․", ".").strip() for p in parts if p and p.strip()]


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9$%][\w'$%.,-]*", CITE.sub("", text))


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1}


def _jaccard(a: str, b: str) -> float:
    x, y = _tokens(a), _tokens(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def _cited(sentence: str, by_id: dict[int, Fact]) -> list[Fact]:
    return [by_id[i] for i in dict.fromkeys(int(n) for n in CITE.findall(sentence)) if i in by_id]


def _is_limit(sentence: str) -> bool:
    low = sentence.lower()
    return not CITE.search(sentence) and any(p in low for p in setting("limit_phrases"))


def _check(id_: str, score: float | None, detail: str, blocking: bool = False) -> dict:
    return {"id": id_, "score": None if score is None else round(max(0.0, min(1.0, score)), 3), "detail": detail, "blocking": blocking}


class Ctx:
    """What the checks need to know about the output being scored."""

    def __init__(self, text, facts, question="", subject="", scope=None, today=None, gaps=None, form=None, checklist=None,
                 established_ids=None):
        self.text, self.facts = text or "", facts
        self.question, self.subject = question or "", subject or ""
        self.scope = scope or {}
        self.today = today or date.today()
        self.gaps = gaps or []
        self.form = form or form_by_id("answer")
        self.checklist = checklist or []
        self.by_id = {f.claim_id: f for f in facts}
        self.established = set(established_ids) if established_ids is not None else set(self.by_id)
        self.sents = sentences(self.text)
        self.cited_sents = [(s, _cited(s, self.by_id)) for s in self.sents]
        self.latest = self.scope.get("intent") == "latest"


# --- time ---

def check_dated(c: Ctx) -> dict:
    want = [(s, f) for s, fs in c.cited_sents for f in fs if f.dated]
    if not want:
        return _check("dated", None, "no cited fact has a date")
    ok = sum(bool(_DATE_WORDS.search(CITE.sub("", s))) for s, _ in want)
    missing = [f"claim {f.claim_id}" for s, f in want if not _DATE_WORDS.search(CITE.sub("", s))]
    none_at_all = not _DATE_WORDS.search(CITE.sub("", c.text))
    return _check("dated", ok / len(want), "every dated event states its date" if ok == len(want)
                  else f"the sentence reporting {', '.join(missing)} does not state its date", blocking=c.latest and none_at_all)


def check_simultaneity(c: Ctx) -> dict:
    words_ = setting("simultaneity_words")
    link = re.compile(r"(?:^|[,;]\s*|\s)(?:" + "|".join(re.escape(w) for w in words_) + r")\b", re.I)
    problems = []
    previous: list[Fact] = []
    for s, fs in c.cited_sents:
        plain = CITE.sub("", s)
        if len(fs) >= 2:
            far = [(a, b) for i, a in enumerate(fs) for b in fs[i + 1:] if apart(a, b) is not False]
            if far and len({m.group(0).lower() for m in _DATE_WORDS.finditer(plain)}) < 2:
                a, b = far[0]
                problems.append(f"one sentence joins claim {a.claim_id} ({a.iso() or 'undated'}) and claim {b.claim_id} ({b.iso() or 'undated'}) without a date for each")
        if fs and previous and link.search(plain):
            far = next(((a, b) for a in previous for b in fs if apart(a, b) is not False and a.claim_id != b.claim_id), None)
            if far:
                a, b = far
                problems.append(f"a simultaneity word joins claim {b.claim_id} ({b.iso() or 'undated'}) to claim {a.claim_id} ({a.iso() or 'undated'}), which did not happen together")
        if fs:
            previous = fs
    if not problems:
        return _check("simultaneity", 1.0, "no events far apart are presented as together")
    return _check("simultaneity", 1 - 0.5 * len(problems), "; ".join(problems), blocking=True)


def check_order(c: Ctx) -> dict:
    seq = []
    for _, fs in c.cited_sents:
        dated = [f for f in fs if f.dated]
        if dated:
            seq.append(dated[0].ordinal)
    if len(seq) < 2:
        return _check("order", None, "fewer than two dated sentences")
    down, up = all(a >= b for a, b in zip(seq, seq[1:])), all(a <= b for a, b in zip(seq, seq[1:]))
    want = "up" if c.scope.get("intent") == "timeline" else "down"
    if (want == "down" and down) or (want == "up" and up):
        return _check("order", 1.0, "dated sentences run in time order, " + ("oldest first" if want == "up" else "newest first"))
    if down or up:
        return _check("order", 0.6, f"dated sentences run {'oldest' if up else 'newest'} first; a {'timeline' if want == 'up' else 'question about the latest'} wants {'oldest' if want == 'up' else 'newest'} first")
    return _check("order", 0.2, "dated sentences are in no time order")


def check_latest_first(c: Ctx) -> dict:
    if not c.latest:
        return _check("latest_first", None, "not a question about the latest")
    dated = [f for f in c.facts if f.dated and f.basis != "stated"] or [f for f in c.facts if f.dated]
    if not dated:
        return _check("latest_first", None, "no dated fact")
    newest = max(dated, key=lambda f: f.ordinal)
    first = next((fs for _, fs in c.cited_sents if fs), [])
    if any(f.claim_id == newest.claim_id or apart(f, newest) is False for f in first if f.dated):
        return _check("latest_first", 1.0, f"the first sentence is about the newest event ({newest.iso()})")
    return _check("latest_first", 0.0, f"the first sentence is not about the newest event (claim {newest.claim_id}, {newest.iso()})")


def check_undated_labelled(c: Ctx) -> dict:
    undated = [(s, f) for s, fs in c.cited_sents for f in fs if not f.dated]
    if not undated:
        return _check("undated_labelled", None, "every cited fact has a date")
    mark = re.compile(r"\bundated\b|\bno (?:reliable )?date\b|\bdate (?:is )?(?:not|unknown|unclear)\b|\bearlier\b|\bbackground\b|\bin (?:19|20)\d{2}\b|\bwhen\b", re.I)
    ok = sum(bool(mark.search(CITE.sub("", s))) for s, _ in undated)
    return _check("undated_labelled", ok / len(undated), "undated facts are labelled as undated or earlier" if ok == len(undated)
                  else f"claim {next(f.claim_id for s, f in undated if not mark.search(s))} has no date and the text does not say so")


def check_implied_link(c: Ctx) -> dict:
    """'The review follows the acquisition': sequence in time is not cause. A link word is fine only if a cited claim says it."""
    bad = []
    for s, fs in c.cited_sents:
        plain = CITE.sub("", s).lower()
        said = " ".join(f"{f.text} {f.quote}" for f in fs).lower()
        bad += [w for w in setting("link_words") if re.search(rf"\b{re.escape(w)}\b", plain) and w not in said]
    return _check("implied_link", 1 - 0.5 * len(bad), "events are joined by time only" if not bad
                  else f"'{bad[0]}' links events in a way no claim states")


def drop_uncited(text: str) -> tuple[str, list[str]]:
    """The text without sentences that state something and cite nothing, and those sentences. A statement no claim backs is removed,
    the same way an invalid citation is; the removed sentences are kept in the report so nothing disappears silently."""
    kept, removed = [], []
    for s in sentences(text):
        (kept if CITE.search(s) or "[invalid citation removed]" in s or _is_limit(s) else removed).append(s)
    return " ".join(kept), removed


def check_vague_time(c: Ctx) -> dict:
    vague = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in setting("vague_time_words")) + r")\b", re.I)
    bad = [s for s in c.sents if vague.search(CITE.sub("", s)) and not _DATE_WORDS.search(CITE.sub("", s))]
    if not c.sents:
        return _check("vague_time", None, "nothing written")
    return _check("vague_time", 1 - 0.34 * len(bad), "no relative time word without a date" if not bad
                  else f"'{vague.search(bad[0]).group(0)}' stands in for a date")


# --- grounding ---

def check_cited(c: Ctx) -> dict:
    claims = [s for s in c.sents if not _is_limit(s)]
    if not claims:
        return _check("cited", None, "nothing written")
    ok = sum(bool(CITE.search(s)) for s in claims)
    bare = next((s for s in claims if not CITE.search(s)), "")
    return _check("cited", ok / len(claims), "every statement is cited" if ok == len(claims)
                  else f"{len(claims) - ok} statement(s) carry no citation, for example '{bare[:70]}'", blocking=ok < len(claims))


def check_evaluative(c: Ctx) -> dict:
    """Praise, weight or interpretation that no claim makes ('a significant enhancement', 'signals a shift')."""
    allowed = " ".join(f"{f.text} {f.value}" for f in c.facts).lower()
    plain = CITE.sub("", c.text).lower()
    hits = [w for w in setting("evaluative_words") if re.search(rf"\b{re.escape(w)}\b", plain) and w not in allowed]
    return _check("evaluative", 1 - 0.34 * len(hits), "no judgement beyond the claims" if not hits
                  else f"'{hits[0]}' is a judgement that no claim makes")


def check_valid_citations(c: Ctx) -> dict:
    ids = [int(n) for n in CITE.findall(c.text)]
    bad = sorted({i for i in ids if i not in c.established}) + (["removed"] if "[invalid citation removed]" in c.text else [])
    if not ids and not bad:
        return _check("valid_citations", None if not c.text else 0.0, "no citations" if c.text else "nothing written")
    return _check("valid_citations", 1.0 if not bad else 0.0, "every citation is an established claim" if not bad
                  else f"citation(s) {bad} are not established claims", blocking=bool(bad))


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(text)}


def check_numbers(c: Ctx) -> dict:
    wrong, total = [], 0
    for s, fs in c.cited_sents:
        nums = _numbers(CITE.sub("", s))
        if not nums or not fs:
            continue
        allowed = set()
        for f in fs:
            allowed |= _numbers(f"{f.text} {f.value} {f.quote} {f.iso()}") | {str(f.date.year), str(f.date.day), str(f.date.month)} if f.date else _numbers(f"{f.text} {f.value} {f.quote}")
        for n in nums:
            total += 1
            if n not in allowed and n.lstrip("0") not in allowed:
                wrong.append(n)
    if not total:
        return _check("numbers", None, "no numbers")
    return _check("numbers", 1 - len(wrong) / total, "every number is in the claims it cites" if not wrong
                  else f"{', '.join(sorted(set(wrong)))} is in none of the cited claims", blocking=bool(wrong))


def check_names(c: Ctx) -> dict:
    known = _tokens(" ".join([c.question, c.subject] + [f"{f.text} {f.value} {f.quote} {f.site}" for f in c.facts] + c.checklist))
    extra = []
    for s in c.sents:
        ws = re.findall(r"[A-Za-z][\w&'-]*", CITE.sub("", s))
        for i, w in enumerate(ws):
            parts = [p for p in re.split(r"[-']", w.lower()) if p]
            if i and w[0].isupper() and any(p not in known and p not in _STOP for p in parts) and w.lower() not in _CALENDAR and len(w) > 2 and not w.isupper():
                extra.append(w)
    extra = list(dict.fromkeys(extra))
    return _check("names", 1 - 0.34 * len(extra), "no name beyond the claims and the question" if not extra
                  else f"{', '.join(extra[:4])} appear in no claim")


# --- materiality ---

def _distinct(facts: list[Fact]) -> list[Fact]:
    kept: list[Fact] = []
    for f in facts:
        if not any(_jaccard(f.text, k.text) >= 0.6 for k in kept):
            kept.append(f)
    return kept


def check_lead(c: Ctx) -> dict:
    first = next((fs for _, fs in c.cited_sents if fs), None)
    if not first or not c.facts:
        return _check("lead", None, "nothing cited")
    if c.latest:
        dated = [f for f in c.facts if f.dated]
        if dated:
            newest = max(dated, key=lambda f: f.ordinal)
            ok = any(f.claim_id == newest.claim_id or apart(f, newest) is False for f in first if f.dated)
            return _check("lead", 1.0 if ok else 0.0, "the lead is the newest event" if ok else f"the lead is not the newest event (claim {newest.claim_id})")
    primary = [f for f in c.facts if f.tier == "primary"]
    if primary and not any(f.tier == "primary" for f in first):
        return _check("lead", 0.4, "a primary-source fact exists but the lead rests on a secondary one")
    return _check("lead", 1.0, "the lead rests on the strongest fact available")


def check_coverage(c: Ctx) -> dict:
    mine = [f for f in c.facts if f.claim_id in c.established]
    distinct = _distinct(mine)
    if not distinct:
        return _check("coverage", None, "no established fact")
    cited = {i for _, fs in c.cited_sents for i in (f.claim_id for f in fs)}
    used = [f for f in distinct if f.claim_id in cited or any(_jaccard(f.text, c.by_id[i].text) >= 0.6 for i in cited if i in c.by_id)]
    return _check("coverage", len(used) / len(distinct), "every distinct established fact is in the text" if len(used) == len(distinct)
                  else f"{len(distinct) - len(used)} established fact(s) are missing, for example claim {next(f.claim_id for f in distinct if f not in used)}")


def _statements(sentence: str, by_id: dict[int, Fact]) -> list[list[Fact]]:
    """The facts each statement in a sentence rests on. Citations that follow each other directly are one statement (one point, cited
    twice); a citation after several more words starts another."""
    groups_ = re.findall(r"(?:\[claim:\d+\])+", sentence)          # runs of citations that touch each other
    return [facts for g in groups_ if (facts := _cited(g, by_id))]


def check_redundancy(c: Ctx) -> dict:
    """The same point written twice: two statements, not one statement with two citations, resting on claims that say the same thing."""
    seen: list[tuple[int, Fact]] = []
    n = 0
    for s, _ in c.cited_sents:
        for facts in _statements(s, c.by_id):
            seen += [(n, f) for f in facts]
            n += 1
    dupes = [(a, b) for i, (na, a) in enumerate(seen) for nb, b in seen[i + 1:]
             if na != nb and a.claim_id != b.claim_id and _jaccard(a.text, b.text) >= 0.6]
    return _check("redundancy", 1 - 0.5 * len(dupes), "no fact is said twice" if not dupes
                  else f"claims {dupes[0][0].claim_id} and {dupes[0][1].claim_id} say the same thing and it is written out twice")


# --- relevance ---

def check_answers(c: Ctx) -> dict:
    cited = list({f.claim_id: f for _, fs in c.cited_sents for f in fs}.values())
    known = [f for f in cited if f.fact is not None]
    if not cited:
        return _check("answers", None, "nothing cited")
    if not known:
        return _check("answers", None, "no fact indexes known")
    off = [f for f in cited if f.fact is None]
    return _check("answers", len(known) / len(cited), "every cited claim answers a needed fact" if not off
                  else f"claim {off[0].claim_id} does not answer a needed fact")


def check_subject(c: Ctx) -> dict:
    sub = _tokens(c.subject)
    if not sub or not c.text:
        return _check("subject", None, "no subject known or nothing written")
    named = bool(sub & _tokens(CITE.sub("", c.text)))
    return _check("subject", 1.0 if named else 0.5, "the output names the subject" if named else f"the output never names '{c.subject}'")


# --- honesty ---

def check_gaps_stated(c: Ctx) -> dict:
    if not c.gaps:
        return _check("gaps_stated", None, "no gaps")
    said = any(p in c.text.lower() for p in setting("limit_phrases"))
    return _check("gaps_stated", 1.0 if said else 0.0, "the output says what is not established" if said
                  else f"{len(c.gaps)} part(s) of the question are not established and the output does not say so")


def check_hedged(c: Ctx) -> dict:
    weak = [(s, f) for s, fs in c.cited_sents for f in fs if (f.corroboration or "").startswith(("single source", "sources disagree"))]
    if not weak:
        return _check("hedged", None, "no weakly backed fact is cited")
    hedge = re.compile(r"\baccording to\b|\breported\b|\bsaid\b|\bstated\b|\bannounced\b|\bunconfirmed\b|\bsingle source\b|\bone source\b|\bdisput|\bdisagree|\bclaims?\b", re.I)
    ok = sum(bool(hedge.search(CITE.sub("", s))) for s, _ in weak)
    return _check("hedged", ok / len(weak), "weakly backed facts are attributed or hedged" if ok == len(weak) else "a single-source or disputed fact is stated flatly")


def check_overclaim(c: Ctx) -> dict:
    hits = [w for w in setting("overclaim_words") if re.search(rf"\b{re.escape(w)}\b", c.text, re.I)]
    return _check("overclaim", 1 - 0.5 * len(hits), "no certainty word" if not hits else f"'{hits[0]}' goes beyond the evidence")


# --- clarity ---

def check_sentence_length(c: Ctx) -> dict:
    if not c.sents:
        return _check("sentence_length", None, "nothing written")
    limit = setting("max_sentence_words")
    long = [s for s in c.sents if len(words(s)) > limit]
    return _check("sentence_length", 1 - len(long) / len(c.sents), f"all sentences are {limit} words or fewer" if not long
                  else f"{len(long)} sentence(s) run over {limit} words")


def check_length(c: Ctx) -> dict:
    n = len(words(c.text))
    if not n:
        return _check("length", None, "nothing written")
    lo, hi = c.form["words"]
    if lo <= n <= hi:
        return _check("length", 1.0, f"{n} words, within the {c.form['label']} range of {lo} to {hi}")
    gap = (lo - n) / lo if n < lo else (n - hi) / hi
    return _check("length", 1 - gap, f"{n} words, outside the {c.form['label']} range of {lo} to {hi}")


def check_filler(c: Ctx) -> dict:
    hits = [p for p in setting("filler_phrases") if re.search(rf"\b{re.escape(p)}\b", c.text, re.I)]
    return _check("filler", 1 - 0.34 * len(hits), "no filler" if not hits else f"filler: '{hits[0]}'")


CHECKS = {k[6:]: v for k, v in globals().items() if k.startswith("check_") and callable(v)}
GRADES_FALLBACK = "F"


def score(text: str, facts: list[Fact], *, question: str = "", subject: str = "", scope: dict | None = None, today: date | None = None,
          gaps: list[str] | None = None, form: dict | None = None, checklist: list[str] | None = None,
          established_ids: set[int] | None = None) -> dict:
    """The matrix applied to one output. `facts` are the established claims it was written from."""
    m = matrix()
    ctx = Ctx(text, facts, question, subject, scope, today, gaps, form, checklist, established_ids)
    if not ctx.text.strip():
        return {"score": None, "grade": None, "form": ctx.form["id"], "dimensions": {}, "blockers": [], "issues": [], "note": "nothing was written"}
    dims, total, weight_used, issues, flagged = {}, 0.0, 0.0, [], {}
    for d in m["dimensions"]:
        results = [CHECKS[name](ctx) for name in d["checks"]]
        scored = [r["score"] for r in results if r["score"] is not None]
        value = sum(scored) / len(scored) if scored else None
        dims[d["id"]] = {"label": d["label"], "weight": d["weight"], "score": None if value is None else round(value, 3),
                         "points": None if value is None else round(value * m["scale"]["max"], 2), "checks": results}
        if value is not None:
            total += d["weight"] * value
            weight_used += d["weight"]
        for r in results:
            if r["score"] is not None and r["score"] < 1:
                issues.append(f"{d['label']}: {r['detail']}")
            if r["blocking"]:
                flagged[r["id"]] = r["detail"]
    overall = 100 * total / weight_used if weight_used else 0.0
    blockers = []
    for b in m["blockers"]:
        if b["from"] in flagged:
            blockers.append({"id": b["id"], "cap": b["cap"], "detail": flagged[b["from"]]})
            overall = min(overall, b["cap"])
    grade = next(g for floor, g in m["grades"] if overall >= floor)
    return {"score": round(overall, 1), "grade": grade, "form": ctx.form["id"], "dimensions": dims, "blockers": blockers, "issues": issues}
