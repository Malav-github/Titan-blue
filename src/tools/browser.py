"""Headless-browser fetch for JavaScript-rendered pages. Slower than HTTP; archives HTML and a screenshot."""

import trafilatura
from playwright.sync_api import sync_playwright

from tools.archive import USER_AGENT, archive_bytes, check_public_url, clip, utc_now


def render_url(url: str, max_chars: int = 12_000, wait_ms: int = 1500) -> dict:
    check_public_url(url)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(wait_ms)
            final_url = page.url
            check_public_url(final_url)
            html = page.content()
            screenshot = page.screenshot(full_page=True)
            status = resp.status if resp else None
        finally:
            browser.close()

    sha256, path = archive_bytes(html.encode("utf-8"), ".html")
    shot_sha256, shot_path = archive_bytes(screenshot, ".png")
    text = trafilatura.extract(html, url=final_url, include_comments=False) or ""
    text, truncated = clip(text, max_chars)
    return {
        "url": url,
        "final_url": final_url,
        "status": status,
        "fetched_at": utc_now(),
        "sha256": sha256,
        "archive_path": path,
        "screenshot_sha256": shot_sha256,
        "screenshot_path": shot_path,
        "text": text,
        "truncated": truncated,
        "method": "browser",
    }
