"""The EDGAR skill (definitions/skills/edgar.json): read SEC filings for a company by code alone.

A local model cannot know a company's CIK, and every EDGAR call it planned for a company the question did not give a CIK
for was a guess (and was refused). So nothing here asks a model:

    resolve(["S&P Global"], tickers)            -> CIK 64040, from the SEC's own company list (archived and cited)
    groups_for(question, checklist)             -> which 8-K items and forms fit the question
    select_filings(submissions, groups, window) -> the filings to read, newest first, each with why it was chosen
    documents(filing, index)                    -> the documents to read in each (the 8-K's Exhibit 99 press release first)

The runner fetches and archives what this chooses and extracts claims from it like any other page.
"""

import json
import re
from datetime import date, timedelta
from functools import lru_cache

from config import PROJECT_ROOT

MANIFEST_PATH = PROJECT_ROOT / "definitions" / "skills" / "edgar.json"
_SUFFIX = re.compile(r"\b(inc|corp|corporation|co|company|ltd|limited|plc|llc|holdings?|group|incorporated|the|n\.?v|s\.?a|ag|se)\b\.?", re.I)


@lru_cache(maxsize=1)
def manifest() -> dict:
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for g in m["item_groups"]:
        re.compile(g["cues"], re.I)
    for x in m["metrics"]:
        re.compile(x["cues"], re.I)
    re.compile(m["exhibit_pattern"], re.I)
    re.compile(m["skip_documents"], re.I)
    if unknown := set(m["default_groups"]) - {g["id"] for g in m["item_groups"]}:
        raise ValueError(f"edgar.json: default_groups names unknown groups {sorted(unknown)}")
    return m


def squash(name: str) -> str:
    """'S&P Global Inc.' and 'S&P Global' -> 'spglobal'."""
    return re.sub(r"[^a-z0-9]", "", _SUFFIX.sub(" ", (name or "").lower()))


def _words(name: str) -> list[str]:
    """'S&P Global Inc.' -> ['s&p', 'global']: the name's words, company suffixes aside."""
    return [w for w in re.split(r"[^a-z0-9&]+", _SUFFIX.sub(" ", (name or "").lower())) if w]


def parse_tickers(raw: bytes | str) -> list[dict]:
    """The SEC's company_tickers.json as [{cik, ticker, title}]."""
    data = json.loads(raw)
    return [{"cik": str(v["cik_str"]).zfill(10), "ticker": v["ticker"], "title": v["title"]} for v in data.values()]


def cik_in_text(text: str) -> str | None:
    """A CIK the question itself gives ('CIK 0000320193', 'CIK: 320193')."""
    m = re.search(r"\bCIK\b\D{0,4}(\d{1,10})\b", text or "", re.I)
    return m.group(1).zfill(10) if m else None


def resolve(names: list[str | None], tickers: list[dict], text: str = "") -> dict:
    """The subject's SEC registrant: {cik, ticker, title, matched_on} or {cik: None, reason}. A name matches a registrant only
    exactly (company suffixes aside) or as its ticker; several different registrants for one name is ambiguous, and then
    nothing is chosen."""
    if given := cik_in_text(text):
        hit = next((t for t in tickers if t["cik"] == given), None)
        return {"cik": given, "ticker": hit["ticker"] if hit else None, "title": hit["title"] if hit else None, "matched_on": "the CIK given in the question"}
    for name in [n for n in names if n]:
        key = squash(name)
        if len(key) < 2:
            continue
        found = {t["cik"]: t for t in tickers if squash(t["title"]) == key}
        how = "is the SEC's registrant name"
        if not found and re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", name.strip()):
            found = {t["cik"]: t for t in tickers if t["ticker"].upper() == name.strip().upper()}
            how = "is the ticker of"
        if not found and len(key) >= 5:
            # 'LyondellBasell' is how people name 'LyondellBasell Industries N.V.': the name's words open the registrant's
            # name. Only when exactly one registrant does ('Meta' opens several, and then nothing is chosen).
            words = _words(name)
            found = {t["cik"]: t for t in tickers if _words(t["title"])[:len(words)] == words}
            how = "opens the SEC's registrant name"
        if len(found) == 1:
            t = next(iter(found.values()))
            return {**t, "matched_on": f"'{name}' {how} '{t['title']}'"}
        if len(found) > 1:
            return {"cik": None, "reason": f"'{name}' matches several SEC registrants: {', '.join(t['title'] for t in found.values())}"}
    shown = ", ".join(f"'{n}'" for n in names if n) or "no name"
    return {"cik": None, "reason": f"{shown} is not an SEC registrant's name or ticker (a private or non-US company, or another name)"}


def named_groups(question: str, checklist: list[str] | None = None) -> list[dict]:
    """The kinds of filing the question's own words point to (results, deals, leadership...); empty when none do."""
    text = f"{question} {' '.join(checklist or [])}"
    return [g for g in manifest()["item_groups"] if re.search(rf"\b(?:{g['cues']})\b", text, re.I)]


def groups_for(question: str, checklist: list[str] | None = None) -> list[dict]:
    """The kinds of filing that fit the question, from its words; the default when nothing points anywhere."""
    m = manifest()
    return named_groups(question, checklist) or [g for g in m["item_groups"] if g["id"] in m["default_groups"]]


def window(scope: dict | None, today: date) -> tuple[date, date]:
    """The filing dates to look at: the years or window the question names, else back from today."""
    scope = scope or {}
    years = sorted(int(y) for y in scope.get("years") or [])
    if years:
        return date(years[0], 1, 1), min(date(years[-1], 12, 31), today)
    try:
        if scope.get("window_start"):
            return date.fromisoformat(str(scope["window_start"])[:10]), date.fromisoformat(str(scope.get("window_end") or today)[:10])
    except ValueError:
        pass
    days = manifest()["lookback_days"]["latest" if scope.get("intent") == "latest" else "default"]
    return today - timedelta(days=days), today


PERIODIC = ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F")


def statement_window(target: dict | None, today: date, fye: str | None = None) -> tuple[date, date]:
    """The filing dates to scan for a statement question (skills/temporal/reporting.py): the announcement window when the
    question names one; for a named period, from its end to the latest a 10-K is filed (about four months); else back from
    today."""
    from skills.temporal import reporting
    t = reporting.Target(**{k: v for k, v in (target or {}).items() if k in reporting.Target.__dataclass_fields__})
    lag = manifest()["statement_filing_lag_days"]
    if t.announced_from:
        return date.fromisoformat(t.announced_from), min(date.fromisoformat(t.announced_to or t.announced_from), today)
    if not t.latest and (pe := reporting.period_end(t, fye)):
        return pe[0] - timedelta(days=reporting.TOLERANCE_DAYS), min(pe[0] + timedelta(days=lag), today)
    if not t.latest and t.fiscal_year:                    # a year with no computable end (a 'fiscal' year, year end unknown)
        return date(t.fiscal_year, 1, 1), min(date(t.fiscal_year, 12, 31) + timedelta(days=lag), today)
    return today - timedelta(days=manifest()["lookback_days"]["latest"]), today


def results_for(filings: list[dict], target: dict | None = None, fye: str | None = None) -> tuple[list[dict], str]:
    """For a statement question: the periodic report (10-Q or 10-K, which prints the full statements) and the earnings
    release (8-K Item 2.02) for the asked period, and why. A named period picks the report whose period of report (the
    SEC's reportDate) is that period's end, and the first release filed after it ends; 'latest' picks the newest of each
    (a release may be newer: results are announced weeks before the 10-K). `filings` are newest first, already in the window."""
    from skills.temporal import reporting
    t = reporting.Target(**{k: v for k, v in (target or {}).items() if k in reporting.Target.__dataclass_fields__})
    periodic = [f for f in filings if f["form"] in PERIODIC]
    releases = [f for f in filings if f["form"].startswith(("8-K", "6-K")) and "2.02" in f["items"]]
    pe = None if t.latest else reporting.period_end(t, fye)
    if pe:
        end = pe[0]
        report = next((f for f in periodic if reporting.near(f.get("report_date"), end)), None)
        after = [f for f in releases if end < date.fromisoformat(f["filed"]) <= reporting.filed_after(end)[1]]
        release = after[-1] if after else None           # newest first: the last one is the first filed after the period ended
        why = f"the period ended about {end} ({pe[2]}): the report whose period of report is that date, and the first earnings release filed after it"
    else:
        report, release = (periodic[0] if periodic else None), (releases[0] if releases else None)
        why = ("the newest periodic report and the newest earnings release" if not t.announced_from else
               f"the newest periodic report and earnings release filed between {t.announced_from} and {t.announced_to}")
    return [f for f in filings if f is report or f is release], why


def select_filings(submissions: dict, groups: list[dict], start: date, end: date, limit: int | None = None) -> list[dict]:
    """Filings in the window whose form or 8-K items fit the groups, newest first, at most `limit` (`max_filings`)."""
    limit = limit or manifest()["max_filings"]
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = {f for g in groups for f in g.get("forms", [])}
    items = {i for g in groups for i in g["items"]}
    out = []
    for i, form in enumerate(recent.get("form", [])):
        try:
            filed = date.fromisoformat(recent["filingDate"][i])
        except (ValueError, IndexError):
            continue
        if not start <= filed <= end:
            continue
        its = [x.strip() for x in (recent.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
        is_8k = form in ("8-K", "8-K/A", "6-K")
        matched = sorted(set(its) & items) if is_8k else []
        if not (matched or form in forms):
            continue
        why = [g["why"] for g in groups if (is_8k and set(its) & set(g["items"])) or form in g.get("forms", [])]
        out.append({"form": form, "filed": recent["filingDate"][i], "report_date": (recent.get("reportDate") or [""] * (i + 1))[i],
                    "items": its, "matched_items": matched, "accession": recent["accessionNumber"][i],
                    "primary": recent["primaryDocument"][i], "description": (recent.get("primaryDocDescription") or [""] * (i + 1))[i],
                    "why": why[0] if why else ""})
        if len(out) >= limit:
            break
    return out


def folder_url(cik: str, accession: str) -> str:
    return manifest()["archives_url"].format(cik=int(cik), accession=accession.replace("-", ""))


def index_url(cik: str, accession: str) -> str:
    """The filing's index page, which lists every document with its official type (8-K, EX-99.1, EX-10.1 ...)."""
    return folder_url(cik, accession) + f"{accession}-index.html"


_ROW = re.compile(r'<a href="(?:/ix\?doc=)?(/Archives/edgar/data/[^"]+)">([^<]+)</a>.*?</td>\s*<td[^>]*>([^<]*)</td>', re.S | re.I)


def index_documents(html: str) -> list[dict]:
    """[{name, type}] from a filing index page, in the filing's own order."""
    return [{"name": name.strip(), "type": kind.strip()} for _, name, kind in _ROW.findall(html or "")]


def documents(filing: dict, cik: str, index: list[dict] | None) -> list[str]:
    """What to read in one filing. An 8-K announcing something carries it as an Exhibit 99 press release, so those come
    first; the 8-K's own text is read too when its items describe the event in the filing itself (a deal, a departure).
    Exhibit types come from the filing's index (`index_documents`); file names are only a fallback, since a company may call
    its press release 'spgi3q2026-earningsrelease.htm'."""
    m = manifest()
    base = folder_url(cik, filing["accession"])
    listed = [x for x in index or [] if not re.search(m["skip_documents"], x["name"], re.I)]
    primary = filing["primary"].split("/")[-1]
    out = []
    if filing["form"].startswith(("8-K", "6-K")):
        exhibits = [x["name"] for x in listed if x["type"].upper().startswith("EX-99")] or \
                   [x["name"] for x in listed if re.search(m["exhibit_pattern"], x["name"], re.I)]
        exhibits = [n for n in exhibits if n.lower().endswith((".htm", ".html", ".pdf"))]
        out += exhibits[:m["max_exhibits_per_filing"]]
        if not exhibits or set(filing["items"]) & {"1.01", "2.01", "2.05", "5.02"}:
            out.append(primary)
    elif not filing["primary"].startswith("xsl"):               # xsl... is a rendering of an XML form, not a document
        out.append(primary)
    return [base + n for n in dict.fromkeys(out)]


def metric_calls(question: str, checklist: list[str] | None = None) -> list[dict]:
    """XBRL concepts the question asks for ('revenue' -> us-gaap Revenues ...), at most `max_metrics`."""
    m = manifest()
    text = f"{question} {' '.join(checklist or [])}"
    return [x for x in m["metrics"] if re.search(rf"\b(?:{x['cues']})\b", text, re.I)][:m["max_metrics"]]
