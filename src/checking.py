"""Verify: check a text someone gives Titan (pasted, uploaded or at a link) against independent sources. A system of its own,
apart from research runs.

    Checker(runner).start(check_id, request)   request: {kind: text | url | file, text?, url?, upload_id?, subject?}

1. The input is archived as a source like any other (a pasted text and an upload are 'upload:' sources, never refetched).
2. It is split into statements by code (sentences.py); each is kept word for word. Code decides which can be checked: a
   statement with a figure, a date, a role or an event can; a forecast or an opinion ('expects', 'believes', 'will') cannot,
   and is said so rather than called unconfirmed.
3. Each checkable statement is recorded as a claim on the input (its quote is the input's own words) and checked by the
   verification agent's checks (verification.py): a second, independent source; a source that gives another figure; newer
   reports; and its figures against the company's financial statements where Titan has read them.
4. The input is never its own evidence: its own site is skipped, and a page holding its words (a copy of the same text on
   another site) is the same document, not a second source.

Statements end corroborated, contradicted, unconfirmed or not checkable. Like any run it can be paused, resumed and stopped,
between statements.
"""

from __future__ import annotations

import json
import mimetypes
import re
from collections import Counter
from pathlib import Path

import sentences
from evidence import normalize
from skills import edgar, roles
from skills.planning.queries import names_in_question
from tools import web
from tools.archive import archive_bytes, utc_now
from verification import Verifier

UPLOAD_SCHEME = "upload:"
# partial: the statement's event is confirmed but a part of it is not (the reason given for a resignation)
OUTCOMES = ("corroborated", "partial", "contradicted", "unconfirmed")
MAX_STATEMENTS = 20                  # checked per input; the rest are listed as not checked
MAX_TEXT_CHARS = 60_000              # of a pasted text
_MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
_OPINION = re.compile(
    r"\b(?:expects?|expected|expecting|anticipat\w*|believ\w*|may|might|could|should|would|plans? to|planning to|intends?|"
    r"intending|aims? to|forecast\w*|outlook|projected|projects? that|estimates? that|likely|hopes?|we think|in our view|"
    r"will(?! have been)|is set to|are set to|poised to|on track to)\b", re.I)
_FACTUAL = re.compile(
    rf"\d|\b{_MONTHS}\b|\b(?:CEO|CFO|COO|CTO|chief \w+ officer|chair\w*|president|founder|director)\b|"
    r"\b(?:acquir\w*|merg\w*|launch\w*|appoint\w*|nam(?:ed|es)|announc\w*|report\w*|filed|founded|completed|closed|sold|bought|"
    r"raised|signed|partner\w*|resign\w*|introduc\w*|releas\w*|opened|agreed|invest\w*|won|awarded|approved)\b", re.I)


def classify(statement: str) -> tuple[bool, str]:
    """(checkable, why) for one statement, by code."""
    words = len(statement.split())
    if words < 5:
        return False, "a heading or a fragment, not a statement"
    if words > 90:
        return False, "too long to check as one statement"
    if _OPINION.search(statement):
        return False, "a forecast or an opinion: it says what someone expects or thinks, not what has happened"
    if not _FACTUAL.search(statement):
        return False, "no figure, date, role or event in it to check"
    return True, ""


def short_title(text: str, limit: int = 70) -> str:
    """The first words of a text, cut at a word, for a title: 'S&P Global reported revenue of $3,678 million…'."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def display_text(raw: str) -> str:
    """The input as shown: each line's whitespace collapsed, as sentences.split does, so statements are found in it."""
    return "\n".join(" ".join(line.split()) for line in raw.splitlines())


_OPENERS = {"the", "a", "an", "we", "our", "this", "that", "these", "in", "on", "at", "for", "by", "as", "it", "its", "after", "since"}


def names_in(sentence: str) -> list[str]:
    """Organisation names in a sentence of a text: runs of capitalised words, the first word included (in a text, unlike a
    question, a sentence often opens with the company's name), without an opening 'The', 'We', 'In' ..."""
    out = []
    for name in names_in_question("so " + sentence):
        words = name.split()
        while words and words[0].lower() in _OPENERS:
            words = words[1:]
        # two words or more, or one written in capitals ('NVIDIA', 'IBM'): generic acronyms ('CEO', 'AI') are already out
        if len(words) > 1 or (words and ("&" in words[0] or (words[0].isupper() and len(words[0]) >= 2))):
            out.append(" ".join(words))
    return out


def subject_of(text: str, given: str | None = None) -> str:
    """Who the text is about: the subject given, else the organisation it names most. The company a role statement names
    ('NVIDIA CEO Jensen Huang ...') counts double: the person in it is not the subject."""
    if given and given.strip():
        return given.strip()
    found: Counter = Counter()
    people = set()
    for sent in sentences.split(text)[:200]:
        if (role := roles.parse(sent)):
            found[role.org] += 2
            people.add(role.person)
        for name in names_in(sent):
            found[name] += 1
    for p in people:
        found.pop(p, None)
    return found.most_common(1)[0][0] if found else ""


class Checker:
    def __init__(self, runner):
        self.r = runner
        self.store, self.runs = runner.store, runner.runs
        self.verifier = Verifier(runner)

    # -- the input ------------------------------------------------------------------------------------------------------

    def read_input(self, request: dict, uploads_dir: Path | None = None) -> tuple[str, str, str]:
        """Archive the input as a source. Returns (source sha256, its address, a short title)."""
        kind = request.get("kind")
        if kind == "text":
            text = (request.get("text") or "").strip()
            if not text:
                raise ValueError("there is no text to check")
            if len(text) > MAX_TEXT_CHARS:
                raise ValueError(f"the text is longer than {MAX_TEXT_CHARS:,} characters; check it in parts")
            body = text.encode("utf-8")
            sha, path = archive_bytes(body, ".txt")
            url = f"{UPLOAD_SCHEME}pasted text {sha[:8]}"
            fetched = {"url": url, "final_url": url, "content_type": "text/plain", "encoding": "utf-8", "fetched_at": utc_now(),
                       "sha256": sha, "archive_path": path, "method": "upload"}
            return self.store.ingest(fetched), url, short_title(text)
        if kind == "url":
            url = (request.get("url") or "").strip()
            if not re.match(r"https?://\S+$", url):
                raise ValueError("give a web address that starts with http:// or https://")
            got = self.r.fetch_fn(url, max_chars=2000)
            if got.get("status") != 200:
                raise ValueError(f"the page could not be read (HTTP {got.get('status')})")
            return self.store.ingest(got), got["final_url"], url
        if kind == "file":
            upload_id = request.get("upload_id") or ""
            if not re.fullmatch(r"[0-9a-f]{64}", upload_id) or uploads_dir is None or not (uploads_dir / f"{upload_id}.bin").exists():
                raise ValueError("the uploaded file was not found; upload it again")
            name = (uploads_dir / f"{upload_id}.name").read_text(encoding="utf-8")
            body = (uploads_dir / f"{upload_id}.bin").read_bytes()
            ctype = "application/pdf" if body[:5] == b"%PDF-" else (mimetypes.guess_type(name)[0] or "text/plain")
            suffix, text = web.extract_text(body, ctype, "utf-8", UPLOAD_SCHEME + name)
            if not normalize(text):
                raise ValueError(f"no readable text in {name} (a scanned document needs OCR first)")
            sha, path = archive_bytes(body, suffix)
            url = UPLOAD_SCHEME + name
            fetched = {"url": url, "final_url": url, "content_type": ctype, "encoding": "utf-8", "fetched_at": utc_now(),
                       "sha256": sha, "archive_path": path, "method": "upload"}
            return self.store.ingest(fetched), url, name
        raise ValueError("say what to check: text, url or file")

    # -- driving --------------------------------------------------------------------------------------------------------

    def start(self, check_id: str, request: dict, uploads_dir: Path | None = None) -> dict:
        sha, url, title = self.read_input(request, uploads_dir)
        raw = Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
        raw = raw.split("\n\n[Page metadata]")[0]                         # the page's own text, not its metadata block
        subject = subject_of(raw, request.get("subject"))
        entity_id = self.store.add_entity(subject or "(not named)", kind="subject")
        shown = display_text(raw)
        found, cursor, checkable_n = [], 0, 0
        for sent in sentences.split(raw):
            ok, why = classify(sent)
            if ok and checkable_n >= MAX_STATEMENTS:
                ok, why = False, f"not checked: only the first {MAX_STATEMENTS} checkable statements are checked at once"
            start = shown.find(sent, cursor)
            span = [start, start + len(sent)] if start >= 0 else None
            if start >= 0:
                cursor = start + len(sent)
            entry = {"n": len(found) + 1, "text": sent, "checkable": ok, "why": why, "span": span}
            if ok:
                rec = self.store.add_claim(entity_id, "statement", "", sent, sha, sent, created_by=f"check:{check_id[:8]}")
                if rec.get("ok"):
                    entry["claim_id"] = rec["claim_id"]
                    checkable_n += 1
                else:
                    entry.update(checkable=False, why=f"could not be recorded: {rec.get('reason')}")
            found.append(entry)
        self.runs.emit(check_id, "check", "run_started", {
            "kind": "check", "question": f"Verify: {title}", "input": {"kind": request.get("kind"), "url": url, "title": title},
            "source_sha256": sha, "subject": subject, "entity_id": entity_id})
        self.runs.emit(check_id, "check", "statements_found", {
            "text": shown, "statements": found, "checkable": checkable_n,
            "why": "split by code; a statement with a figure, a date, a role or an event is checked; forecasts and opinions are not"})
        return self._drive(check_id)

    def resume(self, check_id: str) -> dict:
        self.runs.emit(check_id, "check", "resumed", {})
        return self._drive(check_id)

    def _plan(self, check_id: str) -> tuple[dict, list[dict], set[int]]:
        events = self.runs.events(check_id)
        started = next(e["payload"] for e in events if e["type"] == "run_started")
        found = next(e["payload"]["statements"] for e in events if e["type"] == "statements_found")
        done = {e["payload"]["n"] for e in events if e["type"] == "statement_checked"}
        return started, found, done

    def _drive(self, check_id: str) -> dict:
        started, found, done = self._plan(check_id)
        sha = started["source_sha256"]
        own_words = [normalize(s["text"]).lower() for s in found if s["checkable"]]

        def same_document(other_sha: str) -> bool:
            """A page holding the input's own words is a copy of it, not a second source."""
            text = normalize(Path(self.store.get_source(other_sha)["text_path"]).read_text(encoding="utf-8")).lower()
            hits = sum(w in text for w in own_words)
            return hits >= min(2, len(own_words))

        todo = [s for s in found if s.get("claim_id") and s["n"] not in done]
        for s in todo:
            if wanted := self.r._wanted(check_id):
                if wanted == "stop":
                    return self._finish(check_id, stopped=True)
                self.runs.emit(check_id, "check", "paused", {"by": "you", "statements_left": len([t for t in todo if t["n"] not in done]),
                                                             "note": "statements checked so far are kept; resume carries on"})
                return {"run_id": check_id, "status": "paused"}
            role = roles.parse(s["text"])
            names = names_in(s["text"])
            subject = (role.org if role else "") or started["subject"] or (names[0] if names else "")
            values = {"run_id": check_id, "question": s["text"], "subject": subject, "company": subject, "entity_name": subject,
                      "checklist": [s["text"]], "claim_items": {}, "entity_id": started["entity_id"]}
            notes: list[str] = []
            try:
                found = self._role_check(check_id, role, started["entity_id"]) if role else None
                if found and found["outcome"]:
                    # who holds a role is settled by the company's own filings: no web search with the name as written
                    outcome, checks, notes = found["outcome"], found["checks"], found["notes"]
                else:
                    outcome, checks = self.verifier.verify_claim(values, {"claim_id": s["claim_id"]}, check_id, same_document=same_document)
                    if found:
                        checks, notes = found["checks"] + checks, found["notes"]
            except Exception as exc:                            # one statement's failure must not stop the others
                outcome, checks = "unconfirmed", [{"check": "error", "result": f"{type(exc).__name__}: {str(exc)[:200]}"}]
            # the evidence store knows three outcomes; a statement confirmed only in part is, as a whole, unconfirmed there (the
            # run log keeps 'partial' and why)
            self.store.record_verification(s["claim_id"], check_id, check_id, "unconfirmed" if outcome == "partial" else outcome, checks)
            self.runs.emit(check_id, "check", "statement_checked", {"n": s["n"], "claim_id": s["claim_id"], "outcome": outcome,
                                                                   "checks": checks, "notes": notes,
                                                                   **({"role": role.as_dict()} if role else {})})
            done.add(s["n"])
        return self._finish(check_id)

    # -- who holds a role (skills/roles): the company's own SEC filings ------------------------------------------------

    def _submissions(self, check_id: str, cik: str) -> dict:
        cache = self.__dict__.setdefault("_subs", {})
        if cik not in cache:
            result = self.r.call_api_fn(self.r.registry, "sec_edgar", "submissions", {"cik": cik}, run_id=check_id)
            if not result["ok"]:
                raise RuntimeError(f"EDGAR submissions: HTTP {result['status']}")
            self.store.ingest(result)
            cache[cik] = json.loads(Path(result["archive_path"]).read_bytes())
        return cache[cik]

    def _document(self, url: str) -> tuple[str, str]:
        sha, _ = self.r._fetched(url)
        return sha, Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")

    def _role_check(self, check_id: str, rs: "roles.RoleStatement", entity_id: int) -> dict | None:
        """A statement about who holds a role, checked against the company's own filings: the officer who certified its
        latest 10-Q or 10-K (Exhibit 31.1 for the chief executive, 31.2 for the chief financial officer), and the 8-K Item 5.02
        filings since then, which must report a departure or an appointment within four business days. None when the company
        is not an SEC registrant or EDGAR is not an approved source (the web check carries on alone)."""
        if not any(m.connector_id == "sec_edgar" for _, m in self.r.registry.list_active()):
            return None
        match = edgar.resolve([rs.org], self.r._sec_tickers()[0])
        if not match["cik"]:
            return {"outcome": None, "notes": [], "checks": [{"check": "role_holder", "result": "not checked",
                                                              "detail": f"{match['reason']}: only an SEC registrant's filings are checked"}]}
        cik, company = match["cik"], match["title"]
        recent = (self._submissions(check_id, cik).get("filings") or {}).get("recent") or {}
        rows = [{"form": f, "filed": recent["filingDate"][i], "items": recent["items"][i] or "", "accession": recent["accessionNumber"][i],
                 "primary": recent["primaryDocument"][i]} for i, f in enumerate(recent.get("form", []))]
        periodic = next((r for r in rows if r["form"] in ("10-Q", "10-K", "20-F")), None)
        checks, notes = [], []
        holder = None
        if rs.role in roles.CERTIFYING and periodic:
            _, raw = self.r._fetched(edgar.index_url(cik, periodic["accession"]))     # the filing's index lists each exhibit's type
            index = edgar.index_documents(raw.decode("utf-8", "replace"))
            doc = next((d for d in index if d["type"].upper() == roles.CERTIFYING[rs.role]), None)
            if doc:
                url = edgar.folder_url(cik, periodic["accession"]) + doc["name"]
                sha, text = self._document(url)
                if (c := roles.certifier(text)):
                    holder = {**c, "url": url, "filed": periodic["filed"], "form": periodic["form"]}
                    self.store.add_claim(entity_id, "role_holder", c["name"], c["quote"], sha, c["quote"], created_by=f"check:{check_id[:8]}")
                    checks.append({"check": "role_holder", "result": "named in the filing", "quote": c["quote"], "url": url,
                                   "detail": f"{c['name']}, {c['title'] or roles.LABELS[rs.role]}, certified {company}'s {periodic['form']} filed "
                                             f"{periodic['filed']} ({roles.CERTIFYING[rs.role]}, the {roles.LABELS[rs.role]}'s certification)"})
        name = roles.same_person(rs.person, holder["name"]) if holder else None
        if name:
            checks.append({"check": "name", "result": {"same": "the same person", "spelling": "the same person, misspelled",
                                                      "different": "another person"}[name["match"]], "detail": name["why"]})
            if name["match"] == "spelling":
                notes.append(f"Misspelled name: {name['why']}.")
        # what the 8-K Item 5.02 filings since then report about the person (newest first): a departure, an appointment
        since = periodic["filed"] if periodic else ""
        different = bool(name and name["match"] == "different")
        surnames = [roles.surname_of(rs.person)] + ([roles.surname_of(holder["name"])] if holder and not different else [])
        fives = [r for r in rows if r["form"].startswith("8-K") and "5.02" in r["items"] and r["filed"] >= since][:6]
        texts = []
        for r in fives:
            url = edgar.folder_url(cik, r["accession"]) + r["primary"]
            texts.append((r, url, *self._document(url)))

        def find(kind: str, names: list[str]) -> dict | None:
            for r, url, sha, text in texts:
                if (sent := roles.event_sentence(text, names, kind)):
                    self.store.add_claim(entity_id, f"role_{kind}", "", sent, sha, sent, created_by=f"check:{check_id[:8]}")
                    return {"kind": kind, "sentence": sent, "url": url, "filed": r["filed"], "text": text}
            return None

        written = [roles.surname_of(rs.person)]
        left = find("departure", surnames) if rs.event in ("departure", "holds") else None
        joined = find("appointment", written) if rs.event in ("appointment", "holds") else None
        window = f"since {since}" if since else "in the filing list"
        for kind, ev in (("departure", left), ("appointment", joined)):
            if kind == "departure" and rs.event == "appointment" or kind == "appointment" and rs.event == "departure":
                continue
            if ev:
                checks.append({"check": "role_event", "result": f"{kind} filed", "quote": ev["sentence"], "url": ev["url"],
                               "detail": f"{company}'s Form 8-K, Item 5.02, filed {ev['filed']}"})
            else:
                checks.append({"check": "role_event", "result": f"no {kind} filed",
                               "detail": f"none of {company}'s {len(fives)} Form 8-K Item 5.02 filing(s) {window} reports "
                                         f"{'an' if kind == 'appointment' else 'a'} {kind} for "
                                         f"{rs.person}; such a change must be filed within four business days, so only one in the "
                                         "last four business days could be missing"})
        outcome, event = None, left or joined
        if rs.event == "departure":
            if left:
                outcome = "corroborated"
            elif holder and not different:
                outcome = "contradicted"            # certified as the role holder, and no departure filed since
        elif rs.event == "appointment":
            if joined or (holder and not different):
                outcome = "corroborated"
            elif holder:
                outcome = "contradicted"            # another person certified as the role holder, and no appointment filed since
        else:                                        # holds the role
            if joined:
                outcome = "corroborated"            # appointed since the last certification
            elif holder and not different:
                outcome = "contradicted" if left else "corroborated"
            elif holder:
                outcome = "contradicted"
        if rs.reason and outcome == "corroborated":
            told = event["text"] if event else ""
            if not roles.says_reason(told, rs.reason):
                outcome = "partial"
                checks.append({"check": "reason", "result": "not stated", "detail": f"the filing gives no reason; '{rs.reason}' is not confirmed"})
                notes.append(f"The reason given, '{rs.reason}', is not in the filing.")
        return {"outcome": outcome, "checks": checks, "notes": notes}

    def _finish(self, check_id: str, stopped: bool = False) -> dict:
        counts = self.counts(check_id)
        if stopped:
            self.runs.emit(check_id, "check", "run_stopped", {"reason": "stopped by you; the statements checked so far are kept", **counts})
        else:
            self.runs.emit(check_id, "check", "run_finished", counts)
        return {"run_id": check_id, "status": "stopped" if stopped else "finished", "counts": counts}

    def counts(self, check_id: str) -> dict:
        events = self.runs.events(check_id)
        found = next((e["payload"]["statements"] for e in events if e["type"] == "statements_found"), [])
        outcomes = [e["payload"]["outcome"] for e in events if e["type"] == "statement_checked"]
        return {**{k: outcomes.count(k) for k in OUTCOMES},
                "not_checkable": sum(not s["checkable"] for s in found)}


def describe(runs, check_id: str) -> dict:
    """A check run for the app: the input, the text with each statement placed in it, each statement's verdict and checks."""
    events = runs.events(check_id)
    queued = next((e["payload"] for e in events if e["type"] == "queued"), {})
    started = next((e["payload"] for e in events if e["type"] == "run_started"), {})
    found = next((e["payload"] for e in events if e["type"] == "statements_found"), {})
    results = {e["payload"]["n"]: e["payload"] for e in events if e["type"] == "statement_checked"}
    statements = [{**s, **({k: results[s["n"]].get(k) for k in ("outcome", "checks", "notes", "role")} if s["n"] in results else {})}
                  for s in found.get("statements", [])]
    failure = next((e["payload"].get("error") for e in reversed(events) if e["type"] == "run_failed"), None)
    outcomes = [s.get("outcome") for s in statements]
    return {"run_id": check_id, "kind": "check", "status": runs.status(check_id),
            "title": started.get("question") or queued.get("question") or "Verify",
            "input": started.get("input") or queued.get("input"), "subject": started.get("subject"),
            "text": found.get("text", ""), "statements": statements, "error": failure,
            "counts": {**{k: outcomes.count(k) for k in OUTCOMES},
                       "not_checkable": sum(not s["checkable"] for s in statements),
                       "waiting": sum(1 for s in statements if s["checkable"] and "outcome" not in s)}}
