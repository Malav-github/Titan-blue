"""Compare local models on the jobs Titan actually gives them, scored by code against known answers.

Four tasks, all using the same prompts the pipeline sends:
  extraction  facts + verbatim quotes from a page      -> parse rate, quote-verbatim rate, recall, false claims on a useless page
  support     does this quote back this claim?         -> accuracy and, above all, how often it says "supported" when it is not
  relevance   does this claim answer the needed fact?  -> accuracy, false-positive rate
  manifest    draft an API connector from its docs     -> valid manifest, right host/auth/paths, invented paths

Integrity comes first: a model that waves through unsupported claims is worse than a slower one.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from capability import DraftManifest, draft_prompt, normalize_draft, slug
from config import PROJECT_ROOT
from connectors.manifest import Manifest
from evidence import normalize, verify_quote
import sentences
from runner import NON_FACT, Extraction, Relevance, extraction_prompt, relevance_prompt
from support import CONTEXT_CHARS, PROMPT as SUPPORT_PROMPT, SupportVerdict

EVALS_DIR = PROJECT_ROOT / "evals"
TASKS = ("extraction", "support", "relevance", "manifest")
DEFAULT_MODELS = ["qwen3:8b", "qwen3.5:latest", "gemma4:e4b", "gemma4:12b"]


def load_cases(directory: Path = EVALS_DIR) -> dict:
    cases = json.loads((directory / "cases.json").read_text(encoding="utf-8"))
    cache: dict[str, str] = {}

    def passage(name: str) -> str:
        if name not in cache:
            cache[name] = (directory / "passages" / name).read_text(encoding="utf-8")
        return cache[name]

    for group in ("extraction", "support", "manifest"):
        for case in cases[group]:
            case["text"] = passage(case["passage"])
    return cases


def resolve_quote(passage: str, spec: dict) -> str:
    """{"text": exact} or {"from": a, "to": b} (inclusive span). Fails loudly if the fixture and the quote disagree."""
    if "text" in spec:
        if spec["text"] not in passage:
            raise ValueError(f"fixture quote not in passage: {spec['text'][:60]!r}")
        return spec["text"]
    start = passage.index(spec["from"])
    return passage[start: passage.index(spec["to"], start) + len(spec["to"])]


def timed_call(llm, schema, prompt: str):
    """(result | None, seconds, error | None). A model failing to follow the schema is a result, not a crash."""
    start = time.perf_counter()
    try:
        return llm.with_structured_output(schema).invoke(prompt), time.perf_counter() - start, None
    except Exception as exc:
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {str(exc)[:160]}"


def _rate(num: float, den: float):
    return None if not den else round(num / den, 3)


# --- the four tasks ---------------------------------------------------------------------------------------

def run_extraction(llm, cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        needed = "\n".join(f"{i}. {t}" for i, t in enumerate(case["checklist"], 1))
        out, secs, err = timed_call(llm, Extraction, extraction_prompt(case["question"], needed, case["url"], case["text"]))
        claims = out.claims[:5] if out else []
        units = sentences.split(case["text"])
        pairs = [(c, q) for c in claims for q, _ in sentences.pick_all(units, c.sentences)]   # code copies the chosen sentences
        claims, picked = [c for c, _ in pairs], [q for _, q in pairs]
        quote_ok = [q is not None and verify_quote(case["text"], q)["ok"] for q in picked]
        usable = [ok and len(q.split()) >= 4 and not NON_FACT.search(q) and 1 <= c.needed_fact <= len(case["checklist"])
                  for c, q, ok in zip(claims, picked, quote_ok)]
        quotes = [normalize(q).lower() for q, u in zip(picked, usable) if u]
        hits = sum(any(e.lower() in q for q in quotes) for e in case["expected"])
        rows.append({"id": case["id"], "parsed": out is not None, "claims": len(claims), "quote_ok": sum(quote_ok),
                     "usable": sum(usable), "hits": hits, "expected": len(case["expected"]),
                     "spurious": sum(usable) if case.get("negative") else 0, "seconds": round(secs, 1), "error": err})
    positive = [r for r in rows if r["expected"]]
    return {"cases": rows, "summary": {
        "parse_rate": _rate(sum(r["parsed"] for r in rows), len(rows)),
        "quote_verbatim_rate": _rate(sum(r["quote_ok"] for r in rows), sum(r["claims"] for r in rows)),
        "recall": _rate(sum(r["hits"] for r in positive), sum(r["expected"] for r in positive)),
        "spurious_on_useless_page": sum(r["spurious"] for r in rows),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1)}}


def run_support(llm, cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        quote = resolve_quote(case["text"], case["quote"])
        located = verify_quote(case["text"], quote)
        norm = normalize(case["text"])
        passage = norm[max(0, located["start"] - CONTEXT_CHARS): located["end"] + CONTEXT_CHARS]
        prompt = SUPPORT_PROMPT.format(claim=case["claim"], value=case["value"], quote=normalize(quote), passage=passage)
        out, secs, err = timed_call(llm, SupportVerdict, prompt)
        rows.append({"id": case["id"], "predicted": out.verdict if out else None, "expected": case["expected"],
                     "correct": bool(out and out.verdict in case["expected"]), "seconds": round(secs, 1), "error": err})
    should_pass = [r for r in rows if r["expected"] == ["supported"]]
    should_fail = [r for r in rows if "supported" not in r["expected"]]
    return {"cases": rows, "summary": {
        "parse_rate": _rate(sum(r["predicted"] is not None for r in rows), len(rows)),
        "accuracy": _rate(sum(r["correct"] for r in rows), len(rows)),
        "false_support_rate": _rate(sum(r["predicted"] == "supported" for r in should_fail), len(should_fail)),
        "missed_support_rate": _rate(sum(r["predicted"] != "supported" for r in should_pass), len(should_pass)),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1)}}


def run_relevance(llm, cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        out, secs, err = timed_call(llm, Relevance, relevance_prompt(case["question"], case["fact"], case["claim"]))
        rows.append({"id": case["id"], "predicted": out.addresses if out else None, "expected": case["expected"],
                     "correct": bool(out and out.addresses == case["expected"]), "seconds": round(secs, 1), "error": err})
    negatives = [r for r in rows if not r["expected"]]
    return {"cases": rows, "summary": {
        "parse_rate": _rate(sum(r["predicted"] is not None for r in rows), len(rows)),
        "accuracy": _rate(sum(r["correct"] for r in rows), len(rows)),
        "false_positive_rate": _rate(sum(r["predicted"] is True for r in negatives), len(negatives)),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1)}}


def run_manifest(llm, cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        out, secs, err = timed_call(llm, DraftManifest, draft_prompt(case["name"], case["docs_url"], case["text"]))
        row = {"id": case["id"], "parsed": out is not None, "valid": False, "host_ok": False, "auth_ok": False,
               "paths_found": 0, "paths_expected": len(case["expected"]["paths"]), "extra_paths": 0,
               "seconds": round(secs, 1), "error": err}
        if out:
            try:   # the same cosmetic normalization the capability manager applies, then the real manifest rules
                manifest = Manifest.model_validate({
                    "connector_id": slug(case["name"]), "name": case["name"], "description": "eval", "topics": ["eval"],
                    **normalize_draft(out)})
                exp = case["expected"]
                paths = {e.path for e in manifest.endpoints}
                row.update(valid=True, host_ok=manifest.host == exp["host"],
                           auth_ok=manifest.auth.type == exp["auth_type"] and (exp["auth_name"] is None or manifest.auth.name == exp["auth_name"]),
                           paths_found=len(paths & set(exp["paths"])), extra_paths=len(paths - set(exp["paths"])))
            except Exception as exc:
                row["error"] = f"invalid manifest: {str(exc)[:160]}"
        rows.append(row)
    return {"cases": rows, "summary": {
        "valid_rate": _rate(sum(r["valid"] for r in rows), len(rows)),
        "host_ok": _rate(sum(r["host_ok"] for r in rows), len(rows)),
        "auth_ok": _rate(sum(r["auth_ok"] for r in rows), len(rows)),
        "path_recall": _rate(sum(r["paths_found"] for r in rows), sum(r["paths_expected"] for r in rows)),
        "invented_paths": sum(r["extra_paths"] for r in rows),
        "mean_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1)}}


RUNNERS = {"extraction": run_extraction, "support": run_support, "relevance": run_relevance, "manifest": run_manifest}


# --- running whole models ----------------------------------------------------------------------------------

def _ollama() -> str | None:
    return shutil.which("ollama") or next((str(p) for p in [
        Path.home() / "AppData/Local/Programs/Ollama/ollama.exe"] if p.exists()), None)


def ollama_cmd(*args: str) -> str:
    exe = _ollama()
    if not exe:
        return ""
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu_split(model: str) -> str | None:
    """How Ollama placed the loaded model, e.g. '100% GPU' or '37%/63% CPU/GPU'."""
    for line in ollama_cmd("ps").splitlines()[1:]:
        if line.split()[:1] == [model]:
            m = re.search(r"(\d+%(?:/\d+%)?\s+(?:CPU/GPU|GPU|CPU))", line)
            return m.group(1) if m else line.strip()
    return None


def default_llm_factory(model: str):
    from llm import get_chat_model
    return get_chat_model(task="eval", model=model, temperature=0)   # "eval" is never a Power-mode task: always local


def evaluate(models: list[str], llm_factory=default_llm_factory, tasks=TASKS, cases: dict | None = None, progress=print) -> dict:
    cases = cases or load_cases()
    results = {}
    for model in models:
        progress(f"\n=== {model}")
        llm, per_task, placement, started = llm_factory(model), {}, None, time.perf_counter()
        for task in tasks:
            per_task[task] = RUNNERS[task](llm, cases[task])
            placement = placement or gpu_split(model)       # check once the model is loaded
            progress(f"  {task:11} {per_task[task]['summary']}")
        errors = sorted({r["error"] for t in per_task.values() for r in t["cases"] if r.get("error")})
        results[model] = {"model": model, "tasks": per_task, "placement": placement,
                          "total_seconds": round(time.perf_counter() - started), "distinct_errors": errors[:5]}
        ollama_cmd("stop", model)                           # free the GPU so the next model is measured cleanly
    return results


def ranking(results: dict) -> list[str]:
    """Integrity first: fewest false 'supported' verdicts, then most verbatim quotes, then recall, then speed."""
    def key(model: str):
        t = results[model]["tasks"]
        g = lambda task, name, default: (t.get(task, {}).get("summary", {}).get(name) if t.get(task) else None)  # noqa: E731
        fs, qv, rc = g("support", "false_support_rate", None), g("extraction", "quote_verbatim_rate", None), g("extraction", "recall", None)
        return (1.0 if fs is None else fs, -(qv or 0), -(rc or 0), results[model]["total_seconds"])
    return sorted(results, key=key)


def format_report(results: dict) -> str:
    cols = [("extraction", "parse_rate"), ("extraction", "quote_verbatim_rate"), ("extraction", "recall"),
            ("support", "accuracy"), ("support", "false_support_rate"), ("relevance", "accuracy"),
            ("manifest", "valid_rate"), ("manifest", "path_recall")]
    heads = ["model", "parse", "quotes", "recall", "supp.acc", "FALSE-SUPP", "relev", "manif.ok", "paths", "sec", "placement"]
    lines = [" | ".join(f"{h:>10}" for h in heads)]
    for model in ranking(results):
        r = results[model]
        cells = []
        for task, name in cols:
            v = r["tasks"].get(task, {}).get("summary", {}).get(name) if task in r["tasks"] else None
            cells.append("-" if v is None else f"{v:.2f}")
        lines.append(" | ".join(f"{c:>10}" for c in [model[:10], *cells, str(r["total_seconds"]), (r["placement"] or "?")[:10]]))
    lines.append("\nRanked best first: fewest false 'supported' verdicts, then verbatim quotes, then recall, then speed.")
    return "\n".join(lines)
