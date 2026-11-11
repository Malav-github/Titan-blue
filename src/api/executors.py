"""Executors: the code behind each block. Signature: fn(ctx, inputs, settings) -> {output_port: value}.

Port values that refer to evidence are references (a source hash, a claim id), never copies. The engine
checks every reference against the evidence store, so a block cannot invent a claim or a source.
"""

import hashlib
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

import config
from evidence import MIN_QUOTE_CHARS, UPLOAD_SCHEME, EvidenceStore, normalize, widen_quote
from tools import primary, sandbox, web
from tools.archive import archive_bytes, utc_now

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PREVIEW_CHARS = 500


@dataclass
class Context:
    store: EvidenceStore
    run_id: str
    node_id: str
    emit: Callable[[str, dict], None]          # emit(event_type, payload) -> run log
    llm: Callable[..., object]                 # llm(task, model=None) -> chat model
    connectors: object = None                  # ConnectorRegistry
    uploads_dir: Path = field(default_factory=lambda: config.DATA_DIR / "uploads")


def source_ref(store: EvidenceStore, sha: str) -> dict:
    s = store.get_source(sha)
    text = Path(s["text_path"]).read_text(encoding="utf-8")
    return {"sha256": sha, "url": s["final_url"], "content_type": s["content_type"], "chars": len(text),
            "preview": normalize(text)[:PREVIEW_CHARS]}


def claim_ref(store: EvidenceStore, claim_id: int) -> dict:
    c = store.get_claim(claim_id)
    return {"claim_id": claim_id, "claim": c["claim_text"], "quote": c["quote"], "source_sha256": c["source_sha256"],
            "source_tier": c["source_tier"]}


# -- find ---------------------------------------------------------------------------------------

def search_web(ctx: Context, inputs: dict, settings: dict) -> dict:
    from tools.search import search
    return {"results": search(inputs["query"], settings["max_results"], settings["recency"] or None,
                              settings["site"] or None)}


def resolve_domain(ctx: Context, inputs: dict, settings: dict) -> dict:
    result = primary.resolve_domain(inputs["company"])
    ctx.store.register_resolution(result)       # so claims from a confirmed domain are tiered primary
    return {"resolution": result}


def discover_pages(ctx: Context, inputs: dict, settings: dict) -> dict:
    return {"pages": primary.discover_pages(inputs["domain"], settings["max_pages"])}


# -- read ---------------------------------------------------------------------------------------

def fetch_url(ctx: Context, inputs: dict, settings: dict) -> dict:
    return {"source": source_ref(ctx.store, ctx.store.ingest(web.fetch_url(inputs["url"])))}


def render_url(ctx: Context, inputs: dict, settings: dict) -> dict:
    from tools import browser
    return {"source": source_ref(ctx.store, ctx.store.ingest(browser.render_url(inputs["url"])))}


def save_upload(uploads_dir: Path, name: str, data: bytes) -> dict:
    """Store an uploaded file by hash. Returns the File value that blocks accept."""
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    name = Path(name.replace("\\", "/")).name or "upload"    # no directory parts, ever
    sha = hashlib.sha256(data).hexdigest()
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / f"{sha}.bin").write_bytes(data)
    (uploads_dir / f"{sha}.name").write_text(name, encoding="utf-8")
    return {"upload_id": sha, "name": name, "size": len(data)}


def ingest_file(ctx: Context, inputs: dict, settings: dict) -> dict:
    sha = inputs["file"]["upload_id"]
    name = (ctx.uploads_dir / f"{sha}.name").read_text(encoding="utf-8")
    body = (ctx.uploads_dir / f"{sha}.bin").read_bytes()
    content_type = "application/pdf" if body[:5] == b"%PDF-" else (mimetypes.guess_type(name)[0] or "application/octet-stream")
    suffix, text = web.extract_text(body, content_type, "utf-8", UPLOAD_SCHEME + name)
    if not normalize(text):
        raise ValueError(f"no readable text in {name} (scanned documents need OCR, which is not supported)")
    final = UPLOAD_SCHEME + name
    # ingest() re-reads the archived bytes and re-extracts, so the file must carry its real suffix
    path = Path(archive_bytes(body, suffix)[1])
    fetched = {"url": final, "final_url": final, "content_type": content_type, "encoding": "utf-8",
               "fetched_at": utc_now(), "sha256": sha, "archive_path": str(path), "method": "upload"}
    return {"source": source_ref(ctx.store, ctx.store.ingest(fetched))}


# -- check --------------------------------------------------------------------------------------

def _ask(ctx: Context, task: str, schema: type[BaseModel], prompt: str, model: str | None = None):
    """Structured model call, one retry. Returns None on failure, which is logged on the run."""
    last = None
    for _ in range(2):
        try:
            return ctx.llm(task, model).with_structured_output(schema).invoke(prompt)
        except Exception as exc:
            last = exc
    ctx.emit("llm_failed", {"task": task, "schema": schema.__name__, "error": f"{type(last).__name__}: {str(last)[:200]}"})
    return None


def extract_claims(ctx: Context, inputs: dict, settings: dict) -> dict:
    """Claims by sentence choice (sentences.py): the model gives sentence numbers, code copies the sentences, and the claim
    is the source's own words."""
    import sentences
    import sources
    from runner import NON_FACT, Extraction, extraction_prompt, sentence_chunks, _terms
    sha, need = inputs["source"]["sha256"], normalize(inputs["need"])
    source = ctx.store.get_source(sha)
    if why := sources.not_citable(source["url"], source["final_url"]):
        ctx.emit("source_not_citable", {"url": source["url"], "final_url": source["final_url"], "reason": why})
        return {"claims": []}
    raw = Path(source["text_path"]).read_text(encoding="utf-8")
    text = normalize(raw)
    subject = normalize(inputs.get("subject") or "") or web_host(source["final_url"])
    entity_id = ctx.store.add_entity(subject)
    ids, rejected = [], 0
    seen: set[str] = set()
    for chunk in sentence_chunks(raw, _terms(need), 3):
        units = sentences.split(chunk)
        Extraction.take_dropped(), Extraction.take_dropped_examples()
        extraction = _ask(ctx, "extract", Extraction, extraction_prompt(need, f"1. {need}", source["final_url"], chunk),
                          settings.get("model") or None)
        if dropped := Extraction.take_dropped():
            ctx.emit("claims_malformed", {"count": dropped, "source_url": source["final_url"], "examples": Extraction.take_dropped_examples()})
        for c, quote, why in [(c, q, w) for c in (extraction.claims if extraction else [])[:5]
                              for q, w in sentences.pick_all(units, c.sentences)]:
            if quote is None:
                rejected += 1
                ctx.emit("claim_rejected", {"sentences": c.sentences, "reason": why})
                continue
            if len(normalize(quote)) < MIN_QUOTE_CHARS and (wider := widen_quote(text, quote)):
                ctx.emit("quote_widened", {"from": normalize(quote), "to": wider, "source_url": source["final_url"]})
                quote = wider
            key = normalize(quote)
            if key in seen:
                continue
            seen.add(key)
            if len(quote.split()) < 4 or NON_FACT.search(quote) or c.needed_fact != 1:
                rejected += 1
                ctx.emit("claim_rejected", {"quote": quote, "reason": "not a statement of the needed fact"})
                continue
            from runner import _value_of
            value = c.value if normalize(c.value) and normalize(c.value) in key else _value_of(quote)
            r = ctx.store.add_claim(entity_id, c.predicate or f"fact.{c.needed_fact}", value, key, sha, quote, created_by=f"block:{ctx.run_id[:8]}")
            if r["ok"]:
                ids.append(r["claim_id"])
                ctx.emit("claim_recorded", {"claim_id": r["claim_id"], "claim": key, "quote": key, "page": r.get("page"),
                                            "source_url": source["final_url"], "source_tier": r["source_tier"]})
            else:
                rejected += 1
                ctx.emit("claim_rejected", {"quote": quote, "reason": r["reason"], "closest_passage": r.get("closest_passage")})
    return {"claims": [claim_ref(ctx.store, i) for i in ids]}


def web_host(url: str) -> str:
    return primary.host_of(url) or "unknown"


def check_support(ctx: Context, inputs: dict, settings: dict) -> dict:
    from support import check_claim
    claim_id = inputs["claim"]["claim_id"]
    result = check_claim(ctx.store, claim_id, llm=ctx.llm("claim_support", settings.get("model") or None))
    return {"verdict": result}


def relevance(ctx: Context, inputs: dict, settings: dict) -> dict:
    from runner import Relevance, relevance_prompt
    claim = ctx.store.get_claim(inputs["claim"]["claim_id"])
    need = normalize(inputs["need"])
    v = _ask(ctx, "relevance", Relevance, relevance_prompt(need, need, claim["claim_text"]), settings.get("model") or None)
    if v is None:
        raise RuntimeError("the relevance check failed; see llm_failed in the run log")
    return {"result": {"addresses": v.addresses, "reason": v.reason}}


# -- compute, connect, model --------------------------------------------------------------------

def sandbox_python(ctx: Context, inputs: dict, settings: dict) -> dict:
    return {"result": sandbox.run_python(inputs["code"], timeout=settings["timeout"])}


def sandbox_shell(ctx: Context, inputs: dict, settings: dict) -> dict:
    return {"result": sandbox.run_shell(inputs["script"], timeout=settings["timeout"])}


def api_call(ctx: Context, inputs: dict, settings: dict) -> dict:
    from connectors.caller import call_api
    fetched = call_api(ctx.connectors, settings["connector_id"], settings["endpoint"], inputs.get("params"), run_id=ctx.run_id)
    sha = ctx.store.ingest(fetched)
    return {"response": {"source": source_ref(ctx.store, sha), "status": fetched.get("status")}}


def chat(ctx: Context, inputs: dict, settings: dict) -> dict:
    model = ctx.llm("chat", settings.get("model") or None, settings.get("temperature", 0.2))
    reply = model.invoke(inputs["message"])
    return {"reply": reply.content if hasattr(reply, "content") else str(reply)}


def list_models(ctx: Context, inputs: dict, settings: dict) -> dict:
    from app import ollama_models
    return {"models": {"installed": ollama_models() or [], "default": config.LOCAL_LLM_MODEL,
                       "task_models": config.TASK_MODELS}}


EXECUTORS: dict[str, Callable] = {
    "api.executors:search_web": search_web, "api.executors:resolve_domain": resolve_domain,
    "api.executors:discover_pages": discover_pages, "api.executors:fetch_url": fetch_url,
    "api.executors:render_url": render_url, "api.executors:ingest_file": ingest_file,
    "api.executors:extract_claims": extract_claims, "api.executors:check_support": check_support,
    "api.executors:relevance": relevance, "api.executors:sandbox_python": sandbox_python,
    "api.executors:sandbox_shell": sandbox_shell, "api.executors:api_call": api_call,
    "api.executors:chat": chat, "api.executors:list_models": list_models,
}

# Handled by the engine itself (approval pauses a run) or not wired to the engine yet.
ENGINE_HANDLED = {"api.executors:approve"}
NOT_WIRED_YET = {"api.executors:research_run", "api.executors:capability_request"}
