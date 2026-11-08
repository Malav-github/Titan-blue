"""Numbers as financial statements print them, read by code: grouping that differs by country (1,234,567 / 12,34,567 /
1.234.567,89 / 1 234 567), brackets and minus signs for negatives, dashes for nil, footnote marks, and the scale a table
states once in its caption ("in millions", "Rs. in crore", "All amounts in Philippine Peso").

A value is a Decimal, never a float, so 3,678 million stays exactly 3678000000. Anything that is not clearly a number is
None: a guess at a number is worse than no number.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ontology import financial_statements

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")      # Arabic-Indic and Persian digits
_MINUS = "-−‒–—﹣－"
_NIL = re.compile(rf"^[{_MINUS}]+$|^nil$", re.I)               # printed for zero
# A mark after a number: "1,234 (a)", "1,234*", "1,234¹". Never "(12)": in a statement that is a negative number.
_FOOTNOTE = re.compile(r"(?<=[\d)%])(?:\s*\((?:[a-z]|[ivx]{1,4})\)|\s*\*+|[¹²³⁰-⁹]+)$", re.I)
_CURRENCY = re.compile(r"^(?:US\$|\$|₹|₱|Rs\.?|INR|PHP|USD|Php|P(?=\s?\d))\s?|\s?(?:₹|₱|\$)$")
_NUMBER = re.compile(r"^\d{1,3}(?:([,.  ' ])\d{2,3})*(?:([.,])\d+)?$|^\d+(?:[.,]\d+)?$")


@dataclass(frozen=True)
class Amount:
    value: Decimal          # as printed, before any table scale
    decimals: int           # digits after the decimal point as printed (0 for 3,678)
    negative: bool
    raw: str
    percent: bool = False

    def scaled(self, scale: Decimal) -> Decimal:
        return self.value * (1 if self.percent else scale)


def parse_amount(raw: str, decimal_mark: str | None = None) -> Amount | None:
    """One printed number, or None. `decimal_mark` ('.' or ',') settles 1.234 when a market's convention is known; without it
    a lone separator followed by exactly three digits is read as grouping only when other groups make that certain."""
    text = unicodedata.normalize("NFKC", str(raw)).translate(_DIGITS).strip()
    if not text:
        return None
    if _NIL.match(text):
        return Amount(Decimal(0), 0, False, raw)
    text = _FOOTNOTE.sub("", text).strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1].strip()
    elif text.startswith("(") and not text.count(")"):        # "(1,234" with the closing bracket in the next cell
        negative, text = True, text[1:].strip()
    text = _CURRENCY.sub("", text).strip()
    if text and text[0] in _MINUS:
        negative, text = True, text[1:].strip()
    elif text.startswith("+"):
        text = text[1:].strip()
    text = _CURRENCY.sub("", text).strip()
    percent = text.endswith("%")
    text = text.rstrip("%").strip()
    if not _NUMBER.match(text):
        return None
    value = _to_decimal(text, decimal_mark)
    if value is None:
        return None
    decimals = -value.as_tuple().exponent if value.as_tuple().exponent < 0 else 0
    return Amount(-value if negative else value, decimals, negative, raw, percent)


def _to_decimal(text: str, decimal_mark: str | None) -> Decimal | None:
    seps = [c for c in text if not c.isdigit()]
    if not seps:
        return Decimal(text)
    last = seps[-1]
    tail = len(text) - text.rfind(last) - 1
    if len(set(seps)) == 2:                                    # 1,234.56 or 1.234,56: the last one is the decimal mark
        intpart, frac = text[:text.rfind(last)], text[text.rfind(last) + 1:]
        return _dec(re.sub(r"\D", "", intpart) + "." + frac)
    if len(seps) > 1 or last in "  ' ":              # 1,234,567 or 12,34,567 or 1 234 567: grouping only
        return _dec(re.sub(r"\D", "", text))
    if tail != 3:                                               # 3.5 or 1,25: a decimal mark
        return _dec(text.replace(last, "."))
    if decimal_mark:                                            # 1,234 or 1.234: the market's convention decides
        return _dec(text.replace(last, "") if last != decimal_mark else text.replace(last, "."))
    return _dec(text.replace(last, "")) if last == "," else None   # 1,234 is a thousand everywhere it is printed so; 1.234 is unclear


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# --- the scale and currency a table states once ---------------------------------------------------------------------

@dataclass(frozen=True)
class Scale:
    factor: Decimal | None   # 1, 1e3, 1e5 (lakh), 1e6, 1e7 (crore), 1e9; None when the caption names a currency but no scale
    currency: str | None     # ISO code when the caption names it
    raw: str                 # the words it was read from
    per_share_exempt: bool   # "except per share data": per-share rows are not scaled


def find_scale(text: str) -> Scale | None:
    """The scale and currency a caption states: "(In millions, except per share data)", "(₹ in crore)", "(All amounts in
    Philippine Peso)", "(Amounts in thousands of pesos)". None when it states neither: a table without a stated scale is
    not given one, and a currency named without a scale leaves the scale unknown unless the words say amounts are in
    whole units."""
    vocab = financial_statements()["scales"]
    low = unicodedata.normalize("NFKC", text).lower()
    factor, raw = None, ""
    for word, f in sorted(vocab["words"].items(), key=lambda kv: -len(kv[0])):
        if m := re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low):
            factor, raw = Decimal(f), m[0]
            break
    if factor is None and (m := re.search(r"['’]000s?\b|\b000s\b|\(000\)", low)):
        factor, raw = Decimal(1000), m[0]
    currency = None
    for code, cues in vocab["currencies"].items():
        if any(re.search(rf"(?<![a-z]){re.escape(c.lower())}(?![a-z])", low) for c in cues):
            currency = code
            break
    if factor is None and currency is None:
        return None
    if factor is None and currency and (m := re.search(vocab["whole_units"], low)):
        factor, raw = Decimal(1), m[0]                         # "(All amounts in Philippine Peso)": whole pesos
    return Scale(factor, currency, raw or text.strip()[:80],
                 bool(re.search(r"except (?:for )?(?:per[- ]share|share|eps)", low)))
