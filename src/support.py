"""Claim support check: does the verified quote actually back the claim as worded?

Quote verification proves the source says X. It does not prove the claim says X. This check
judges that gap using only the quote and its surrounding passage, never outside knowledge, and
records a verdict. A model produces the judgement; code adds two things a model can't be trusted
with: the passage is cut from the verified text by offset, and numbers and identifiers in the
claim must appear in the quote or the verdict cannot be better than `partially_supported`.
"""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from evidence import EvidenceStore, normalize
from skills.temporal.periods import period_conflicts

PROMPT_VERSION = "support-v1"
CONTEXT_CHARS = 700

PROMPT = """You check whether a quoted passage from a web page supports a claim. Judge ONLY from the passage below.
Use no outside knowledge, no assumptions about what the company or document probably means.

A claim is `supported` only if every factual element of it (who, what, which identifier, number, date, \
relationship) is stated or directly entailed by the passage.
It is `partially_supported` if some elements are stated but others (an inference, a link between two things, \
a scope, a causal or "this is the same as that" step) are not.
It is `not_supported` if the passage does not state the claim, or only suggests it, or contradicts it.

Be strict about linking claims: two things appearing near each other or sharing a topic is NOT a statement that \
one is the other. List each unsupported element in `unsupported_parts`.

CLAIM: {claim}
VALUE: {value}
QUOTE: {quote}

SURROUNDING PASSAGE (the quote sits inside it):
{passage}
"""


class SupportVerdict(BaseModel):
    verdict: Literal["supported", "partially_supported", "not_supported"]
    reasoning: str = Field(description="One or two sentences naming what the passage does and does not state")
    unsupported_parts: list[str] = Field(default_factory=list, description="Elements of the claim the passage does not state")


def claim_tokens(*texts: str) -> list[str]:
    """Numbers, money, percentages and identifiers (e.g. WO2021037851A1) that must be traceable to the quote."""
    found = []
    for text in texts:
        found += re.findall(r"[A-Z]{1,4}\d[\dA-Z/.\-]{3,}|\$?\d[\d,.]*%?", text)
    return sorted({t.rstrip(".,") for t in found if len(t.strip("$,.%")) >= 1})


def missing_tokens(claim_text: str, value: str, quote: str, source_url: str = "") -> list[str]:
    """Tokens in the claim that appear neither in the quote nor in the URL of the document it comes from
    (a claim about 'patent X' may rest on a page whose address names X)."""
    q = normalize(f"{quote} {source_url}").lower()
    return [t for t in claim_tokens(claim_text, value) if t.lower() not in q]


def passage_for(store: EvidenceStore, claim) -> str:
    source = store.get_source(claim["source_sha256"])
    text = normalize(Path(source["text_path"]).read_text(encoding="utf-8"))
    lo, hi = max(0, claim["quote_start"] - CONTEXT_CHARS), claim["quote_end"] + CONTEXT_CHARS
    return text[lo:hi]


def check_claim(store: EvidenceStore, claim_id: int, llm=None, checker: str | None = None) -> dict:
    """Run the check, record it, return it. `llm` is any LangChain chat model (default: task 'claim_support')."""
    claim = store.get_claim(claim_id)
    if claim is None:
        raise ValueError(f"unknown claim {claim_id}")
    if normalize(claim["claim_text"]) == normalize(claim["quote"]):
        # the claim is the source's own sentence, copied by code (extraction by sentence choice): it says exactly what the
        # quote says, so there is nothing for a model to judge
        reasoning = "The claim is the quoted sentence itself, copied from the source by code; nothing in it is reworded."
        check_id = store.record_support_check(claim_id, "supported", reasoning, [], [], "code:verbatim", PROMPT_VERSION)
        return {"check_id": check_id, "claim_id": claim_id, "verdict": "supported", "reasoning": reasoning,
                "unsupported_parts": [], "missing_tokens": [], "checker": "code:verbatim"}
    if llm is None:
        from llm import get_chat_model
        llm = get_chat_model(task="claim_support", temperature=0)
    checker = checker or getattr(llm, "model", None) or getattr(llm, "model_name", None) or type(llm).__name__

    prompt = PROMPT.format(claim=claim["claim_text"], value=claim["value"], quote=claim["quote"],
                           passage=passage_for(store, claim))
    result: SupportVerdict = llm.with_structured_output(SupportVerdict).invoke(prompt)

    verdict, reasoning, unsupported = result.verdict, result.reasoning, list(result.unsupported_parts)
    source_url = store.get_source(claim["source_sha256"])["final_url"]
    missing = missing_tokens(claim["claim_text"], claim["value"], claim["quote"], source_url)
    if missing and verdict == "supported":  # code overrides the model: untraceable numbers or ids
        verdict = "partially_supported"
        unsupported.append(f"not in the quote: {', '.join(missing)}")
        reasoning += " [Downgraded by code: the claim contains tokens absent from the quote.]"

    if mismatch := period_conflicts(claim["claim_text"], claim["quote"]):   # a claim about one period cannot rest on a quote about another
        unsupported += mismatch
        if verdict == "supported":
            verdict = "partially_supported"
            reasoning += " [Downgraded by code: the claim's period differs from the quote's.]"

    check_id = store.record_support_check(claim_id, verdict, reasoning, unsupported, missing, str(checker), PROMPT_VERSION)
    return {"check_id": check_id, "claim_id": claim_id, "verdict": verdict, "reasoning": reasoning,
            "unsupported_parts": unsupported, "missing_tokens": missing, "checker": str(checker)}
