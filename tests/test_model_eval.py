import pytest

import model_eval
import re

from capability import DraftEndpoint, DraftManifest, DraftParam
from evidence import verify_quote
from model_eval import ranking, resolve_quote
from runner import Relevance
from tests.scripted import ExtractedClaim, Extraction, resolved
from support import SupportVerdict

CASES = model_eval.load_cases()


class Scripted:
    def __init__(self, handlers):
        self.handlers = handlers

    def with_structured_output(self, schema):
        handler = self.handlers[schema.__name__]

        class Call:
            def invoke(self_, prompt):
                out = handler(prompt)
                if isinstance(out, Exception):
                    raise out
                return resolved(out, prompt)
        return Call()


def sentence_around(text, needle):
    i = text.lower().index(needle.lower())
    return text[max(0, i - 40): i + len(needle) + 40]


def perfect():
    def extraction(prompt):
        case = next(c for c in CASES["extraction"] if c["url"] in prompt)
        return Extraction(claims=[
            ExtractedClaim(predicate="p", needed_fact=1, quote=e) for e in case["expected"]])      # the sentence that holds it

    def support(prompt):
        case = next(c for c in CASES["support"] if f"CLAIM: {c['claim']}" in prompt)
        return SupportVerdict(verdict=case["expected"][0], reasoning="scripted")

    def relevance(prompt):
        case = next(c for c in CASES["relevance"] if f"CLAIM: {c['claim']}" in prompt)
        return Relevance(addresses=case["expected"], reason="scripted")

    def manifest(prompt):
        case = next(c for c in CASES["manifest"] if f"API: {c['name']}" in prompt)
        exp = case["expected"]
        return DraftManifest(base_url=f"https://{exp['host']}/v1", auth_type=exp["auth_type"], auth_name=exp["auth_name"],
                             endpoints=[DraftEndpoint(name=f"ep{i}", path=p, description="d",
                                                      params=[DraftParam(name=n, **{"in": "path"}) for n in re.findall(r"{(\w+)}", p)])
                                        for i, p in enumerate(exp["paths"])],
                             smoke_endpoint="ep0", smoke_params={})

    return Scripted({"Extraction": extraction, "SupportVerdict": support, "Relevance": relevance, "DraftManifest": manifest})


def gullible():
    """Says yes to everything and invents quotes."""
    return Scripted({
        "Extraction": lambda p: Extraction(claims=[ExtractedClaim(predicate="p", value="x", claim="Everything here is completely true.",
                                                                  quote="a sentence that is not on the page at all", needed_fact=1)]),
        "SupportVerdict": lambda p: SupportVerdict(verdict="supported", reasoning="sure"),
        "Relevance": lambda p: Relevance(addresses=True, reason="sure"),
        "DraftManifest": lambda p: ValueError("bad json")})


def broken():
    boom = lambda p: RuntimeError("model does not support this")  # noqa: E731
    return Scripted({k: boom for k in ("Extraction", "SupportVerdict", "Relevance", "DraftManifest")})


def run(models, monkeypatch):
    monkeypatch.setattr(model_eval, "ollama_cmd", lambda *a: "")
    return model_eval.evaluate(list(models), llm_factory=lambda m: models[m], cases=CASES, progress=lambda *_: None)


# --- the fixtures themselves ------------------------------------------------------------------------------

@pytest.mark.parametrize("case", CASES["support"], ids=lambda c: c["id"])
def test_every_support_quote_really_is_on_its_page(case):
    quote = resolve_quote(case["text"], case["quote"])
    assert verify_quote(case["text"], quote)["ok"]


@pytest.mark.parametrize("case", [c for c in CASES["extraction"] if c["expected"]], ids=lambda c: c["id"])
def test_every_expected_fact_is_actually_in_its_passage(case):
    for needle in case["expected"]:
        assert needle.lower() in case["text"].lower()


def test_fixture_labels_are_balanced_enough_to_measure_both_error_directions():
    expected = [tuple(c["expected"]) for c in CASES["support"]]
    assert expected.count(("supported",)) >= 5 and sum(1 for e in expected if "supported" not in e) >= 5
    assert any(not c["expected"] for c in CASES["relevance"]) and any(c["expected"] for c in CASES["relevance"])
    assert any(c.get("negative") for c in CASES["extraction"])


# --- scoring -----------------------------------------------------------------------------------------------

def test_a_perfect_model_scores_perfectly(monkeypatch):
    r = run({"perfect": perfect()}, monkeypatch)["perfect"]["tasks"]
    assert r["extraction"]["summary"]["quote_verbatim_rate"] == 1.0 and r["extraction"]["summary"]["recall"] == 1.0
    assert r["extraction"]["summary"]["spurious_on_useless_page"] == 0
    assert r["support"]["summary"]["accuracy"] == 1.0 and r["support"]["summary"]["false_support_rate"] == 0.0
    assert r["relevance"]["summary"]["accuracy"] == 1.0
    assert r["manifest"]["summary"] == {**r["manifest"]["summary"], "valid_rate": 1.0, "path_recall": 1.0, "invented_paths": 0}


def test_a_model_that_waves_everything_through_is_caught(monkeypatch):
    r = run({"gullible": gullible()}, monkeypatch)["gullible"]["tasks"]
    assert r["support"]["summary"]["false_support_rate"] == 1.0           # said "supported" to every unsupported claim
    assert r["relevance"]["summary"]["false_positive_rate"] == 1.0
    assert r["extraction"]["summary"]["quote_verbatim_rate"] == 0.0       # every quote was invented
    assert r["manifest"]["summary"]["valid_rate"] == 0.0


def test_a_broken_model_counts_as_failures_and_reports_why(monkeypatch):
    results = run({"broken": broken()}, monkeypatch)
    t = results["broken"]["tasks"]
    assert t["support"]["summary"]["parse_rate"] == 0.0 and t["extraction"]["summary"]["parse_rate"] == 0.0
    assert "does not support this" in results["broken"]["distinct_errors"][0]


def test_ranking_puts_integrity_ahead_of_speed(monkeypatch):
    results = run({"gullible": gullible(), "perfect": perfect(), "broken": broken()}, monkeypatch)
    results["gullible"]["total_seconds"] = 1                              # even if it is the fastest
    results["perfect"]["total_seconds"] = 999
    assert ranking(results)[0] == "perfect"
    assert "perfect" in model_eval.format_report(results)


def test_a_manifest_that_invents_paths_is_penalised(monkeypatch):
    extra = Scripted({"DraftManifest": lambda p: DraftManifest(
        base_url="https://api.patentx.example/v1", auth_type="header", auth_name="X-Api-Key", smoke_endpoint="aa",
        endpoints=[DraftEndpoint(name="aa", path="/patents", description="d"), DraftEndpoint(name="bb", path="/made/up", description="d")])})
    summary = model_eval.run_manifest(extra, [c for c in CASES["manifest"] if c["id"] == "patentx"])["summary"]
    assert summary["invented_paths"] == 1 and summary["path_recall"] == round(1 / 3, 3)
