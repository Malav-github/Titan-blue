#!/usr/bin/env python
"""Smoke test for the model layer: plain answer, tool call, schema-constrained output."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import requests
from langchain_core.tools import tool
from pydantic import BaseModel, Field

import config
from llm import get_chat_model


@tool
def get_company_founding_year(company: str) -> int:
    """Look up the year a company was founded."""
    return {"anthropic": 2021}.get(company.lower(), 0)


class Claim(BaseModel):
    subject: str = Field(description="Entity the claim is about")
    predicate: str = Field(description="Relation, e.g. 'founded_in'")
    object: str = Field(description="Value of the relation")


def check(name, fn):
    start = time.perf_counter()
    try:
        detail = fn()
        print(f"  PASS  {name} ({time.perf_counter() - start:.1f}s): {detail}")
        return True
    except Exception as e:
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        return False


def main() -> int:
    print(f"Model: {config.LOCAL_LLM_MODEL} | mode: {config.LLM_MODE} | ctx: {config.OLLAMA_NUM_CTX}")

    try:
        names = [m["name"] for m in requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=5).json()["models"]]
    except requests.exceptions.ConnectionError:
        print("Ollama is not running. Start the Ollama app or run: ollama serve")
        return 1
    if config.LOCAL_LLM_MODEL not in names:
        print(f"Model {config.LOCAL_LLM_MODEL} not pulled. Run: ollama pull {config.LOCAL_LLM_MODEL}")
        return 1

    model = get_chat_model()

    def plain():
        return model.invoke("Reply with exactly one word: ready").content.strip()

    def tool_call():
        msg = model.bind_tools([get_company_founding_year]).invoke("When was Anthropic founded? Use the tool.")
        if not msg.tool_calls:
            raise AssertionError(f"no tool call, got text: {msg.content[:80]!r}")
        call = msg.tool_calls[0]
        return f"{call['name']}({call['args']})"

    def structured():
        claim = model.with_structured_output(Claim).invoke(
            "Extract the claim: 'Anthropic was founded in 2021.'"
        )
        if not isinstance(claim, Claim):
            raise AssertionError(f"expected Claim, got {type(claim).__name__}")
        return claim.model_dump()

    results = [check("plain answer", plain), check("tool call", tool_call), check("structured output", structured)]
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
