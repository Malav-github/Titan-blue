"""Plain HTTP fetch with readable-text extraction. Fast; use for static pages and APIs."""

from dataclasses import dataclass

import httpx
import trafilatura

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


def download(url: str, timeout: float = 20.0, max_bytes: int = MAX_BYTES) -> Download:
    """GET with the SSRF guard on every hop and a size cap. Returns bytes untouched."""
    def guard(request: httpx.Request) -> None:
        check_public_url(str(request.url))  # re-checked on every redirect hop

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [guard]},
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
                            resp.encoding or "utf-8")


def fetch_url(url: str, max_chars: int = 12_000, timeout: float = 20.0) -> dict:
    dl = download(url, timeout)
    body, content_type, final_url, encoding = dl.body, dl.content_type, dl.final_url, dl.encoding

    if "html" in content_type:
        suffix = ".html"
        html = body.decode(encoding, errors="replace")
        text = trafilatura.extract(html, url=final_url, include_comments=False) or ""
    elif content_type.startswith("text/") or content_type.endswith("json"):
        suffix = ".json" if content_type.endswith("json") else ".txt"
        text = body.decode(encoding, errors="replace")
    else:
        suffix, text = ".bin", ""

    sha256, path = archive_bytes(body, suffix)
    text, truncated = clip(text, max_chars)
    return {
        "url": url,
        "final_url": final_url,
        "status": dl.status,
        "content_type": content_type,
        "fetched_at": utc_now(),
        "sha256": sha256,
        "archive_path": path,
        "text": text,
        "truncated": truncated,
        "method": "http",
    }
