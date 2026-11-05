"""The golden set: written outputs that a person has scored, with reasons, so the matrix's code checks can be tuned until they agree.

    python -m skills.writing.golden capture --run <run id> --id sp-latest --note "..."   take a finished run's output from the local API
    python -m skills.writing.golden sheet sp-latest                                      show the output, its facts and the code's score, for scoring by hand
    python -m skills.writing.golden grade sp-latest --time 1 --time-why "..." --grounding 4 --grounding-why "..." ...
    python -m skills.writing.golden compare                                              how far the code is from the person, per dimension

Each case keeps the claim rows the output was written from, so the code's score is recomputed whenever the checks change; the
person's scores are never touched. The reason for each score is required: it is what tells the checks what to look for.
"""

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime, timezone

from config import PROJECT_ROOT
from skills.writing import build_facts, matrix, score
from skills.writing.kinds import choose_form, written_as

GOLDEN_PATH = PROJECT_ROOT / "evals" / "writing_golden.json"
_ROW_KEYS = ("claim_id", "claim", "value", "quote", "times", "source_url", "source_date", "source_date_kind", "source_tier", "corroboration", "fact")


def load(path=GOLDEN_PATH) -> dict:
    if not path.exists():
        return {"about": "Written outputs scored by a person, with reasons. See src/skills/writing/golden.py.", "cases": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save(data: dict, path=GOLDEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def case_from_report(report: dict, case_id: str, note: str = "", today: date | None = None) -> dict:
    """A golden case from a finished run's report: the question, the written output and the claims it was written from."""
    t = report.get("time") or {}
    rows = [{k: r.get(k) for k in _ROW_KEYS} | {"established": r in report.get("established", [])}
            for r in report.get("established", []) + report.get("partial", [])]
    return {"id": case_id, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "question": report.get("question", ""), "subject": report.get("subject", ""),
            "today": t.get("today") or (today or date.today()).isoformat(), "scope": {"intent": t.get("scope") or ""},
            "gaps": report.get("gaps", []), "output": report.get("summary", ""), "claims": rows, "notes": note, "human": None}


def facts_of(case: dict):
    return build_facts([r for r in case["claims"]])


def auto_score(case: dict) -> dict:
    form = written_as(choose_form(case["question"], case["scope"]))
    ids = {r["claim_id"] for r in case["claims"] if r.get("established")}
    return score(case["output"], facts_of(case), question=case["question"], subject=case.get("subject", ""), scope=case["scope"],
                 today=date.fromisoformat(case["today"]), gaps=case.get("gaps"), form=form, established_ids=ids)


def grade(case: dict, scores: dict[str, int], reasons: dict[str, str], grader: str = "") -> dict:
    """Record a person's scores (0 to 5 for each dimension) with a reason for each."""
    top = matrix()["scale"]["max"]
    ids = [d["id"] for d in matrix()["dimensions"]]
    missing = [i for i in ids if i not in scores]
    if missing:
        raise ValueError(f"score every dimension; missing: {', '.join(missing)}")
    for i in ids:
        if not (isinstance(scores[i], int) and 0 <= scores[i] <= top):
            raise ValueError(f"{i}: a score is a whole number from 0 to {top}")
        if not (reasons.get(i) or "").strip():
            raise ValueError(f"{i}: give the reason for the score; the checks are tuned from it")
    case["human"] = {"graded_by": grader, "graded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "scores": {i: scores[i] for i in ids}, "reasons": {i: reasons[i].strip() for i in ids}}
    return case


def overall_of(scores: dict[str, int]) -> float:
    m = matrix()
    return round(sum(d["weight"] * scores[d["id"]] / m["scale"]["max"] for d in m["dimensions"]), 1)


def compare(data: dict | None = None) -> dict:
    """The code against the person, on graded cases: mean absolute gap in points, bias (code minus person) and how often they are within one point."""
    data = data or load()
    graded = [c for c in data["cases"] if c.get("human")]
    dims = {}
    for d in matrix()["dimensions"]:
        pairs = []
        for c in graded:
            auto = auto_score(c)["dimensions"].get(d["id"], {}).get("points")
            if auto is not None:
                pairs.append((c["id"], auto, c["human"]["scores"][d["id"]]))
        if pairs:
            gaps = [a - h for _, a, h in pairs]
            dims[d["id"]] = {"cases": len(pairs), "mean_abs_gap": round(sum(abs(g) for g in gaps) / len(gaps), 2),
                             "bias": round(sum(gaps) / len(gaps), 2), "within_one": round(sum(abs(g) <= 1 for g in gaps) / len(gaps), 2),
                             "worst": max(pairs, key=lambda p: abs(p[1] - p[2]))[0]}
    overall = [(c["id"], auto_score(c)["score"], overall_of(c["human"]["scores"])) for c in graded]
    return {"graded": len(graded), "ungraded": len(data["cases"]) - len(graded), "dimensions": dims,
            "overall": [{"id": i, "code": a, "person": h, "gap": None if a is None else round(a - h, 1)} for i, a, h in overall]}


def _fetch_report(run_id: str, base: str) -> dict:
    token = (PROJECT_ROOT / "data" / "api_token.txt").read_text().strip()
    req = urllib.request.Request(f"{base}/api/runs/{run_id}", headers={"X-Titan-Token": token})
    body = json.load(urllib.request.urlopen(req, timeout=15))
    if not body.get("report"):
        raise SystemExit(f"run {run_id} has no report (status: {body.get('status')})")
    return body["report"]


def _sheet(case: dict) -> str:
    res = auto_score(case)
    out = [f"CASE {case['id']}   today {case['today']}   scope {case['scope'].get('intent') or '-'}", f"QUESTION: {case['question']}", "",
           "OUTPUT:", case["output"] or "(nothing written)", "", "FACTS IT WAS WRITTEN FROM (date, where the date came from):"]
    for f in facts_of(case):
        out.append(f"  [claim:{f.claim_id}] {f.iso() or 'undated':10} {f.basis:9} {f.text[:110]}")
    out += ["", f"CODE SCORE: {res['score']} ({res['grade']})  blockers: {[b['id'] for b in res['blockers']] or 'none'}"]
    for k, d in res["dimensions"].items():
        out.append(f"  {k:11} {d['points']}/5   " + " | ".join(f"{c['id']}={c['score']}" for c in d["checks"] if c["score"] is not None))
    out += ["", "ISSUES THE CODE FOUND:"] + [f"  - {i}" for i in res["issues"]] + ["", "SCORE EACH DIMENSION 0-5 WITH A REASON:"]
    for d in matrix()["dimensions"]:
        out.append(f"  {d['id']}: {d['question']}")
    return "\n".join(out)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m skills.writing.golden")
    sub = p.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--run")
    cap.add_argument("--file")
    cap.add_argument("--id", required=True)
    cap.add_argument("--note", default="")
    cap.add_argument("--api", default="http://127.0.0.1:8000")
    sub.add_parser("sheet").add_argument("id")
    g = sub.add_parser("grade")
    g.add_argument("id")
    g.add_argument("--by", default="")
    for d in matrix()["dimensions"]:
        g.add_argument(f"--{d['id']}", type=int, required=True)
        g.add_argument(f"--{d['id']}-why", required=True)
    sub.add_parser("compare")
    a = p.parse_args(argv)
    data = load()
    if a.cmd == "capture":
        report = _fetch_report(a.run, a.api) if a.run else json.loads(open(a.file, encoding="utf-8").read())
        if any(c["id"] == a.id for c in data["cases"]):
            raise SystemExit(f"a case called {a.id} exists")
        data["cases"].append(case_from_report(report, a.id, a.note))
        save(data)
        print(f"captured {a.id}; next: python -m skills.writing.golden sheet {a.id}")
        return
    if a.cmd == "compare":
        print(json.dumps(compare(data), indent=1))
        return
    case = next((c for c in data["cases"] if c["id"] == a.id), None)
    if not case:
        raise SystemExit(f"no case called {a.id}")
    if a.cmd == "sheet":
        print(_sheet(case))
        return
    ids = [d["id"] for d in matrix()["dimensions"]]
    grade(case, {i: getattr(a, i) for i in ids}, {i: getattr(a, f"{i}_why") for i in ids}, a.by)
    save(data)
    print(f"graded {a.id}: person {overall_of(case['human']['scores'])}, code {auto_score(case)['score']}")


if __name__ == "__main__":
    main(sys.argv[1:])
