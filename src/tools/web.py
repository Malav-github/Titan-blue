"""Plain HTTP fetch with readable-text extraction. Fast; use for static pages and APIs."""

import io
from dataclasses import dataclass, field

import httpx
import trafilatura
from bs4 import BeautifulSoup

import policy
from connectors.render import render_json
from skills.temporal.extract import collect_dates
from tools.archive import USER_AGENT, archive_bytes, check_public_url, clip, utc_now

MAX_BYTES = 5_000_000


@dataclass
class Download:
    """Raw response, before any interpretation."""
    body: bytes
    status: int
    final_url: str
    content_type: str
    encoding: str
    headers: dict = field(default_factory=dict)      # only Last-Modified and Date, lower-case keys


def download(url: str, timeout: float = 20.0, max_bytes: int = MAX_BYTES) -> Download:
    """GET with the SSRF guard on every hop and a size cap. Returns bytes untouched."""
    def guard(request: httpx.Request) -> None:
        check_public_url(str(request.url))  # re-checked on every redirect hop
        policy.before_request(str(request.url))                      # the site's usage rules, on every hop
        request.headers.update(policy.request_headers(str(request.url)))

    def after(response: httpx.Response) -> None:
        policy.after_response(str(response.request.url), response.status_code)

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [guard], "response": [after]},
    ) as client:
        with client.stream("GET", url) as resp:
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"Response larger than {max_bytes} bytes: {url}")
                chunks.append(chunk)
            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            return Download(b"".join(chunks), resp.status_code, str(resp.url), content_type,
                            resp.encoding or "utf-8", {k: resp.headers[k] for k in ("last-modified", "date") if k in resp.headers})


META_KEYS = ("dc.", "citation_", "og:", "description", "twitter:title", "article:")
MAX_META = 40


def meta_block(html: str) -> str:
    """Page metadata that article extraction drops (e.g. a patent's assignee), as plain `name: value` lines."""
    lines = []
    for tag in BeautifulSoup(html, "html.parser").find_all("meta"):
        name = (tag.get("name") or tag.get("property") or "").strip()
        content = (tag.get("content") or "").strip()
        if content and name.lower().startswith(META_KEYS):
            scheme = tag.get("scheme")
            lines.append(f"{name}{f' ({scheme})' if scheme else ''}: {content}")
    return "\n".join(lines[:MAX_META])


MAX_PDF_PAGES = 100


def pdf_text(body: bytes) -> str:
    """Text layer of a PDF (no OCR). Scanned documents give an empty string, which callers treat as unreadable."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(body))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES])
    except Exception:
        return ""


def extract_text(body: bytes, content_type: str, encoding: str, url: str) -> tuple[str, str]:
    """(file suffix, readable text). The one extractor used for fetching AND for evidence ingestion."""
    if "html" in content_type:
        html = body.decode(encoding, errors="replace")
        text = trafilatura.extract(html, url=url, include_comments=False) or ""
        meta = meta_block(html)
        return ".html", f"{text}\n\n[Page metadata]\n{meta}" if meta else text
    if content_type == "application/pdf" or body[:5] == b"%PDF-":
        return ".pdf", pdf_text(body)
    if content_type.startswith("text/") or content_type.endswith("json"):
        text = body.decode(encoding, errors="replace")
        if content_type.endswith("json") and (readable := render_json(url, text)):
            return ".json", readable        # the raw JSON stays archived and hashed; claims are quoted from this readable form
        return (".json" if content_type.endswith("json") else ".txt"), text
    return ".bin", ""


EXTRACTOR = f"trafilatura {trafilatura.__version__} + meta-v1 + pypdf + json-render-v1"


def fetch_url(url: str, max_chars: int = 12_000, timeout: float = 20.0) -> dict:
    dl = download(url, timeout)
    body, content_type, final_url, encoding = dl.body, dl.content_type, dl.final_url, dl.encoding

    suffix, text = extract_text(body, content_type, encoding, final_url)
    html = body.decode(encoding, errors="replace") if "html" in content_type else None
    dates = collect_dates(html, dl.headers, final_url)                # what the page says about when it was written

    sha256, path = archive_bytes(body, suffix)
    text, truncated = clip(text, max_chars)
    return {
        "url": url,
        "final_url": final_url,
        "status": dl.status,
        "content_type": content_type,
        "encoding": encoding,
        "dates": [d.as_dict() for d in dates],
        "fetched_at": utc_now(),
        "sha256": sha256,
        "archive_path": path,
        "text": text,
        "truncated": truncated,
        "method": "http",
    }
