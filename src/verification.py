"""The verification agent: a separate run, on request, that checks a finished research run's answer. A research run answers as
soon as a strong enough source states each fact (settling.py); this is the slower look behind it.

For each claim of the answer, by code, with no model:
  source         the source is fetched again at its exact address: is the quote still there?
  second_source  a search for an independent source: a sentence that names the subject and gives the same value (recorded as a
                 claim, word for word, with its tier)
  contradiction  a sentence from another site that names the subject and the fact but gives a different figure
  newer          search results dated after the source: newer reports to look at (pointers, not evidence)
  figures        the claim's figures against the company's financial statements

Each claim ends `corroborated` (a second independent source agrees and nothing contradicts it), `contradicted` (the quote is gone,
a figure differs from the statements, or another source gives a different figure) or `unconfirmed`. Results are kept in the
evidence store (claim_verifications) and the run log. Like a research run it can be paused, resumed and stopped: between claims.
"""

import re
import uuid
from pathlib import Path

import corroboration
import sentences
import settling
from evidence import normalize
from skills.statements import figures

VERSION = "verify-v1"
MAX_RESULTS = 8          # search results looked at per claim
MAX_READ = 3             # of those, pages read for a second source
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z&'-]{3,}")
_STOP = {"with", "that", "this", "from", "have", "were", "which", "their", "about", "after", "before", "said", "says", "also",
         "into", "than", "they", "will", "would", "been", "being", "over", "under", "more", "most", "such", "only", "other",
         "company", "announced", "reported", "quarter", "year", "million", "billion", "percent"}


class Verifier:
    def __init__(self, runner):
        self.r = runner
        self.store, self.runs = runner.store, runner.runs

    # -- driving ------------------------------------------------------------------------------------------------------

    def start(self, of_run_id: str, claim_ids: list[int] | None = None, verify_id: str | None = None) -> dict:
        snap = self.r.snapshot(of_run_id)
        values, report = snap["values"], snap["values"].get("report") or {}
        rows = report.get("established", []) + report.get("partial", [])
        if claim_ids:
            rows = [r for r in rows if r["claim_id"] in set(claim_ids)]
        verify_id = verify_id or uuid.uuid4().hex
        self.runs.emit(verify_id, "verify", "run_started", {
            "kind": "verify", "of_run": of_run_id, "question": f"Verify: {values.get('question', '')}",
            "subject": values.get("subject"), "claims": [r["claim_id"] for r in rows]})
        return self._drive(verify_id)

    def resume(self, verify_id: str) -> dict:
        self.runs.emit(verify_id, "verify", "resumed", {})
        return self._drive(verify_id)

    def _plan(self, verify_id: str) -> tuple[dict, set[int]]:
        events = self.runs.events(verify_id)
        started = next(e["payload"] for e in events if e["type"] == "run_started")
        done = {e["payload"]["claim_id"] for e in events if e["type"] == "claim_verified"}
        return started, done

    def _drive(self, verify_id: str) -> dict:
        started, done = self._plan(verify_id)
        of_run = started["of_run"]
        values = self.r.snapshot(of_run)["values"]
        report = values.get("report") or {}
        rows = {r["claim_id"]: r for r in report.get("established", []) + report.get("partial", [])}
        for cid in started["claims"]:
            if cid in done:
                continue                                    # verified before a pause
            if wanted := self.r._wanted(verify_id):
                if wanted == "stop":
                    return self._finish(verify_id, of_run, stopped=True)
                self.runs.emit(verify_id, "verify", "paused", {"by": "you", "claims_left": len([c for c in started["claims"] if c not in done]),
                                                              "note": "claims verified so far are kept; resume carries on"})
                return {"run_id": verify_id, "status": "paused"}
            try:
                outcome, checks = self.verify_claim(values, rows.get(cid) or {"claim_id": cid}, verify_id)
            except Exception as exc:                        # one claim's failure must not stop the others
                outcome, checks = "unconfirmed", [{"check": "error", "result": f"{type(exc).__name__}: {str(exc)[:200]}"}]
            self.store.record_verification(cid, verify_id, of_run, outcome, checks)
            self.runs.emit(verify_id, "verify", "claim_verified", {"claim_id": cid, "outcome": outcome, "checks": checks})
            done.add(cid)
        return self._finish(verify_id, of_run)

    def _finish(self, verify_id: str, of_run: str, stopped: bool = False) -> dict:
        found = [v for v in self.store.verifications_of_run(of_run) if v["verify_run_id"] == verify_id]
        counts = {k: sum(v["outcome"] == k for v in found) for k in ("corroborated", "contradicted", "unconfirmed")}
        if stopped:
            self.runs.emit(verify_id, "verify", "run_stopped", {"reason": "stopped by you; the claims verified so far are kept", **counts})
        else:
            self.runs.emit(verify_id, "verify", "run_finished", counts)
        return {"run_id": verify_id, "status": "stopped" if stopped else "finished", "counts": counts}

    # -- one claim ------------------------------------------------------------------------------------------------------

    def verify_claim(self, values: dict, row: dict, verify_id: str) -> tuple[str, list[dict]]:
        claim = self.store.get_claim(row["claim_id"])
        source = self.store.get_source(claim["source_sha256"])
        names = self.r._names(values) or [values.get("subject") or ""]
        fact = (values.get("checklist") or [""])[values.get("claim_items", {}).get(str(claim["claim_id"]), 0)] if values.get("checklist") else ""
        checks = [self._source_check(claim, source)]
        target = self._target(values, claim, fact)
        second, contradiction, newer = self._search_checks(values, claim, source, names, fact, verify_id, target)
        checks += [second, contradiction, newer, self._figures_check(values, claim, names, target)]
        gone = checks[0]["result"] in ("quote gone", "unreachable")
        if checks[0]["result"] == "quote gone" or contradiction["result"] == "found" or checks[-1]["result"] == "differs":
            outcome = "contradicted"
        elif second["result"] == "found" and not gone:
            outcome = "corroborated"
        else:
            outcome = "unconfirmed"
        return outcome, checks

    def _source_check(self, claim, source) -> dict:
        url = source["final_url"]
        if url.startswith("upload:"):
            return {"check": "source", "result": "not refetchable", "detail": "your own uploaded file"}
        try:
            got = self.r.fetch_fn(url, max_chars=2000)
        except Exception as exc:
            return {"check": "source", "result": "unreachable", "detail": f"{type(exc).__name__}: {str(exc)[:160]}", "url": url}
        if got.get("status") != 200:
            return {"check": "source", "result": "unreachable", "detail": f"HTTP {got.get('status')}", "url": url}
        sha = self.store.ingest(got)
        if sha == claim["source_sha256"]:
            return {"check": "source", "result": "unchanged", "detail": "the page is byte for byte the one quoted", "url": url}
        text = normalize(Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8"))
        if normalize(claim["quote"]) in text:
            return {"check": "source", "result": "quote still there", "detail": "the page has changed, the quoted words have not",
                    "url": url, "source_sha256": sha}
        return {"check": "source", "result": "quote gone", "detail": "the page no longer has the quoted words", "url": url, "source_sha256": sha}

    def _terms(self, claim, fact: str, names: list[str]) -> tuple[list[str], list[str]]:
        """The claim's figures, and the words that say what it is about (from the needed fact and the quote)."""
        numbers = [n for n in _NUMBER.findall(claim["quote"]) if len(n.replace(",", "").replace(".", "")) >= 2]
        name_words = {w.lower() for n in names for w in _WORD.findall(n or "")}
        words = [w.lower() for w in _WORD.findall(f"{fact} {claim['quote']}") if w.lower() not in _STOP and w.lower() not in name_words]
        return numbers, list(dict.fromkeys(words))[:6]

    def _target(self, values: dict, claim, fact: str) -> dict | None:
        """The amount a claim states, read by code: from the statement row it is (a table row's figure for the period asked),
        or from the first reported amount in its sentence. Compared as an amount, not as text: '$3.68 billion' is 3,678 million."""
        from decimal import Decimal
        if "|" in claim["quote"] and (row := self.r._statement_row(values, claim, fact)):
            f = row["figure"]
            return {"concepts": [row["concept"]], "value": abs(Decimal(f["value"])), "half": Decimal(0), "label": row["label"],
                    "period": (f["period_end"][:7], f["period_months"]) if f.get("period_end") else None, "printed": f["printed"],
                    "statement_row": True}
        for m in figures.mentions(claim["quote"]):
            if not m.not_compared and not m.change and m.value is not None:
                fye = figures.fiscal_year_end(self.store.statement_figures(entity_id=values.get("entity_id")) if values.get("entity_id") else [])
                placed = next((pl[:2] for p in m.periods if (pl := figures.place(p, fye))), None)
                return {"concepts": m.concepts, "value": abs(m.value), "half": m.half, "label": m.phrase, "period": placed,
                        "period_labels": {p.label for p in m.periods}, "printed": m.raw, "statement_row": False}
        return None

    def _amounts(self, sent: str, target: dict, fye) -> tuple[bool, str | None]:
        """(agrees, a contradicting amount) for one sentence against the claim's amount: the same line item at the same
        amount (at the precision written) agrees; the same line item for the same period at another amount contradicts."""
        disagree = None
        for m in figures.mentions(sent):
            if m.not_compared or m.change or m.value is None or not set(m.concepts) & set(target["concepts"]):
                continue
            if abs(abs(m.value) - target["value"]) <= m.half + target["half"]:
                return True, None
            placed = [pl[:2] for p in m.periods if (pl := figures.place(p, fye))]
            named = {p.label for p in m.periods}
            # the same period: placed by the company's year when it can be, else named the same ("Q3 2026" and "Q3 2026")
            if (target.get("period") and target["period"] in placed) or (target.get("period_labels") and target["period_labels"] & named):
                disagree = m.raw
        return False, disagree

    def _search_checks(self, values: dict, claim, source, names: list[str], fact: str, verify_id: str,
                       target: dict | None = None) -> tuple[dict, dict, dict]:
        value = (claim["value"] or "").strip()
        numbers, words = self._terms(claim, fact, names)
        if target:                                     # an amount: search for the line item and the period, not the table's cells
            fact_words = [w.lower() for w in _WORD.findall(fact) if w.lower() not in _STOP and not any(
                w.lower() in (n or "").lower() for n in names)]
            words = list(dict.fromkeys([*target["label"].lower().split(), *fact_words]))[:6]
            from skills.temporal.periods import find_periods
            when = next((p.raw for p in find_periods(f"{fact} {values.get('question', '')}")), "")    # "Q3 2026": the period asked
            query = " ".join([names[0] if names else "", *words[:3], when]).strip()
        else:
            anchor = value or (numbers[0] if numbers else "")
            query = " ".join([names[0] if names else "", *words[:4], anchor]).strip()
        fye = figures.fiscal_year_end(self.store.statement_figures(entity_id=values.get("entity_id")) if values.get("entity_id") else [])
        none = {"check": "second_source", "result": "not found", "query": query}
        try:
            hits = (self.r.search_fn(query, max_results=MAX_RESULTS) or {}).get("results", [])
        except Exception as exc:
            err = f"search failed: {type(exc).__name__}"
            return ({**none, "detail": err}, {"check": "contradiction", "result": "not checked", "detail": err},
                    {"check": "newer", "result": "not checked", "detail": err})
        own = corroboration.site(source["final_url"])
        others = [h for h in hits if corroboration.site(h["url"]) != own]
        newer = self._newer(values, claim, others)
        second, contra = none, {"check": "contradiction", "result": "none found", "query": query}
        for h in others[:MAX_READ]:
            try:
                got = self.r.fetch_fn(h["url"], max_chars=2000)
                if got.get("status") != 200:
                    continue
                sha = self.store.ingest(got)
            except Exception:
                continue
            groups = self.r._documents([claim["source_sha256"], sha])
            if groups.get(sha) == groups.get(claim["source_sha256"]):
                continue                                        # a copy of the same document is not a second source
            raw = Path(self.store.get_source(sha)["text_path"]).read_text(encoding="utf-8")
            for sent in sentences.split(raw):
                low = sent.lower()
                if not any((n or "").lower() in low for n in names if n) or not any(w in low for w in words[:4]):
                    continue
                if target:
                    agrees, other = self._amounts(sent, target, fye)
                else:
                    agrees = (value and normalize(value).lower() in normalize(sent).lower()) or (
                        numbers and sum(n in sent for n in numbers) >= min(2, len(numbers)))
                    other = None
                theirs = [n for n in _NUMBER.findall(sent) if len(n.replace(",", "").replace(".", "")) >= 2]
                if agrees and second is none:
                    recorded = self.store.add_claim(claim["entity_id"], claim["predicate"], value if value in sent else "", normalize(sent), sha,
                                                    sent, created_by=f"verify:{verify_id[:8]}")
                    strength = self.r._strong(values, sha)
                    blog = self.r._blog(values, self.store.get_source(sha)["final_url"])
                    second = {"check": "second_source", "result": "found", "quote": normalize(sent), "url": self.store.get_source(sha)["final_url"],
                              "claim_id": recorded.get("claim_id"),
                              "tier": (strength or {}).get("tier") or (blog or {}).get("tier") or 3,
                              "tier_label": (strength or {}).get("tier_label") or settling.tier_label((blog or {}).get("tier") or 3)}
                elif target and other and contra["result"] != "found":
                    contra = {"check": "contradiction", "result": "found", "quote": normalize(sent), "url": self.store.get_source(sha)["final_url"],
                              "detail": f"gives {other} for the same line item and period where the claim gives {target['printed']}; "
                                        "read both before relying on either"}
                elif not target and numbers and theirs and not agrees and contra["result"] != "found" and _same_kind(numbers, theirs):
                    contra = {"check": "contradiction", "result": "found", "quote": normalize(sent), "url": self.store.get_source(sha)["final_url"],
                              "detail": f"gives {', '.join(theirs[:3])} where the claim gives {', '.join(numbers[:3])}; read both before relying on either"}
            if second is not none and contra["result"] == "found":
                break
        return second, contra, newer

    def _newer(self, values: dict, claim, hits: list[dict]) -> dict:
        from skills.temporal.extract import best_date
        best = best_date(self.r._source_dates(claim["source_sha256"]))
        since = (best.value[:10] if best else "") or ""
        later = [{"title": (h.get("title") or "")[:120], "url": h["url"], "published": str(h.get("published"))[:10]}
                 for h in hits if h.get("published") and since and str(h["published"])[:10] > since]
        return {"check": "newer", "result": f"{len(later)} newer report(s)" if later else "none seen",
                "detail": f"search results dated after the source ({since})" if since else "the source has no date to compare with",
                "items": later[:5]}

    def _figures_check(self, values: dict, claim, names: list[str], target: dict | None = None) -> dict:
        if target and target.get("statement_row"):
            return {"check": "figures", "result": "is the statement",
                    "detail": f"the quote is the '{target['label']}' row of the statements read by code ({target['printed']})"}
        index = self.store.statement_figures(entity_id=values.get("entity_id")) if values.get("entity_id") else []
        if not index:
            return {"check": "figures", "result": "no statements read", "detail": "no financial statements of the company were read"}
        results = figures.check(claim["quote"], index, names=tuple(n for n in names if n))
        if not results:
            return {"check": "figures", "result": "no figures", "detail": "the quote states no figure the statements cover"}
        worst = next((r for r in results if r["verdict"] == "differs"), None) or next(
            (r for r in results if r["verdict"] in ("matches", "found")), results[0])
        return {"check": "figures", "result": worst["verdict"], "detail": worst["why"]}


def _same_kind(ours: list[str], theirs: list[str]) -> bool:
    """Two figures that could be the same quantity: of the same order of magnitude (a revenue and a year are not)."""
    def size(n):
        try:
            return float(n.replace(",", ""))
        except ValueError:
            return None
    a, b = [size(n) for n in ours if size(n) is not None], [size(n) for n in theirs if size(n) is not None]
    years = lambda x: 1900 <= x <= 2100 and float(x).is_integer()      # noqa: E731
    a, b = [x for x in a if not years(x)], [x for x in b if not years(x)]
    return bool(a and b) and any(0.2 <= (x / y if y else 0) <= 5 for x in a for y in b)
