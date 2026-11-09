"""Scanned pages read by two OCR engines at once, so that a figure counts only when both read it the same.

A page with no text layer is rendered at 300 DPI and given to Tesseract and to RapidOCR (PaddleOCR's models through ONNX)
in parallel; the two are built differently, so they rarely make the same mistake. Their words are paired by where they sit
on the page. A number must match character for character, brackets and grouping included. Each number then carries both
readings and both confidences into the same table reader and arithmetic checks as any PDF, and is graded by `grade` in
__init__: verified, agreed, or doubt (said loudly, with the place on the page to look at).

Both engines run locally and send nothing anywhere. Tesseract is a Windows program (installed with winget,
UB-Mannheim.TesseractOCR); when it is missing, OCR is refused with that message rather than done with one engine.
"""

import csv
import io
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from .numbers import parse_amount

DPI = 300
MIN_CHARS = 20                     # a page with fewer characters in its text layer is treated as scanned
TESSERACT_PATHS = [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]
MIN_CONFIDENCE = {"tesseract": 0.80, "rapidocr": 0.90}    # below this a reading is doubtful even if both agree


class OcrUnavailable(RuntimeError):
    pass


def tesseract_path() -> str | None:
    return os.environ.get("TITAN_TESSERACT") or shutil.which("tesseract") or next((p for p in TESSERACT_PATHS if os.path.exists(p)), None)


def available() -> str | None:
    """None when both engines are ready; otherwise what is missing, in words a person can act on."""
    if not tesseract_path():
        return "Tesseract is not installed (winget install UB-Mannheim.TesseractOCR, or set TITAN_TESSERACT to tesseract.exe)"
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return "RapidOCR is not installed (uv sync)"
    return None


def needs_ocr(page) -> bool:
    """A pdfplumber page whose text layer is empty or nearly so."""
    return len(page.chars) < MIN_CHARS


def _render(pdf_bytes: bytes, page_index: int):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return doc[page_index].render(scale=DPI / 72, grayscale=True).to_pil()
    finally:
        doc.close()


def _tesseract(png: str) -> list[dict]:
    r = subprocess.run([tesseract_path(), png, "stdout", "--psm", "6", "-c", "preserve_interword_spaces=1", "tsv"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    out = []
    for row in csv.DictReader(io.StringIO(r.stdout), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if not text or float(row["conf"]) < 0:
            continue
        x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        out.append({"text": text, "x0": x, "x1": x + w, "top": y, "bottom": y + h, "conf": float(row["conf"]) / 100, "engine": "tesseract"})
    return out


@lru_cache(maxsize=1)
def _rapid_engine():
    import logging
    from rapidocr import RapidOCR
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    return RapidOCR()


def _rapid(png: str) -> list[dict]:
    """RapidOCR finds text lines; a line holding several words is split, each word given its share of the line's width."""
    res = _rapid_engine()(png)
    out = []
    for box, text, score in zip(res.boxes if res.boxes is not None else [], res.txts or [], res.scores or []):
        xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]       # numpy floats are not JSON: stored with each figure
        x0, x1, top, bottom = min(xs), max(xs), min(ys), max(ys)
        tokens = text.split()
        if not tokens:
            continue
        per_char = (x1 - x0) / max(len(text), 1)
        pos = 0
        for tok in tokens:
            start = text.index(tok, pos)
            pos = start + len(tok)
            out.append({"text": tok, "x0": x0 + start * per_char, "x1": x0 + pos * per_char, "top": top, "bottom": bottom,
                        "conf": float(score), "engine": "rapidocr"})
    return out


def _is_number(text: str) -> bool:
    return parse_amount(text) is not None and any(c.isdigit() for c in text)


def _same_place(a: dict, b: dict) -> bool:
    """Two readings of one printed word: their boxes overlap across most of the narrower one, on the same line."""
    overlap = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    narrow = min(a["x1"] - a["x0"], b["x1"] - b["x0"]) or 1
    ca, cb = (a["top"] + a["bottom"]) / 2, (b["top"] + b["bottom"]) / 2
    return overlap >= 0.5 * narrow and abs(ca - cb) <= max(a["bottom"] - a["top"], b["bottom"] - b["top"]) * 0.6


_BORDERS = "|[]{}¦"                 # table rules that OCR reads as characters


def _norm(text: str) -> str:
    return "".join(text.split()).strip(_BORDERS)


def combine(tess: list[dict], rapid: list[dict]) -> list[dict]:
    """One word list for the table reader. RapidOCR gives the layout (labels and headers: it reads shaded header bands that
    defeat Tesseract). Every number is paired with whatever Tesseract read at the same place, even if that is garbled
    ("6674.72]"), and carries both readings; a number only one engine saw carries one. Tesseract's words fill in only
    where RapidOCR found nothing."""
    words, used = [], set()
    for r in rapid:
        here = sorted((k for k, t in enumerate(tess) if k not in used and _same_place(r, t)), key=lambda k: tess[k]["x0"])
        if _is_number(r["text"]):
            t = None
            if here:
                used.update(here)
                t = {"text": "".join(tess[k]["text"] for k in here), "conf": min(tess[k]["conf"] for k in here)}
            words.append({**r, "ocr": _reading(t, r)})
        else:
            used.update(here)
            if (text := _label_text(r["text"])):
                words.append({**r, "text": text, "ocr": None})
    for k, t in enumerate(tess):
        if k in used or any(_same_place(t, w) for w in words):
            continue
        if _is_number(t["text"]):
            words.append({**t, "ocr": _reading(t, None)})          # a number only Tesseract read
        elif (text := _label_text(t["text"])):
            words.append({**t, "text": text, "ocr": None})
    return sorted(words, key=lambda w: (w["top"], w["x0"]))


def _label_text(text: str) -> str:
    return text.strip(_BORDERS + " ")


def _reading(t: dict | None, r: dict | None) -> dict:
    agree = bool(t and r and _norm(t["text"]) == _norm(r["text"]))
    text = (r or t)["text"]
    return {"tesseract": t["text"] if t else None, "rapidocr": r["text"] if r else None,
            "conf": {"tesseract": round(t["conf"], 3) if t else None, "rapidocr": round(r["conf"], 3) if r else None},
            "agree": agree, "text": text}


def read_page(pdf_bytes: bytes, page_index: int) -> list[dict]:
    """Both engines on one rendered page, in parallel. Words come back in PDF points (1/72 inch), like a text layer."""
    if why := available():
        raise OcrUnavailable(why)
    image = _render(pdf_bytes, page_index)
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "page.png")
        image.save(png)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ft, fr = pool.submit(_tesseract, png), pool.submit(_rapid, png)
            tess, rapid = ft.result(), fr.result()
    k = 72 / DPI
    words = combine(tess, rapid)
    for w in words:
        for key in ("x0", "x1", "top", "bottom"):
            w[key] = w[key] * k
        if w.get("ocr"):
            w["ocr"]["page_box"] = [round(w["x0"], 1), round(w["top"], 1), round(w["x1"], 1), round(w["bottom"], 1)]
    return words
