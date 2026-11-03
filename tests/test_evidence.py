import hashlib
import sqlite3
from pathlib import Path

import pytest

from evidence import EvidenceStore, normalize, verify_quote

PAGE = """<html><body><article>
<h1>Acme Pricing</h1>
<p>The Starter plan costs $29 per month, billed annually.</p>
<p>The Team plan costs $99 per month and includes   priority support.</p>
</article></body></html>"""

QUOTE = "The Starter plan costs $29 per month, billed annually."


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    return EvidenceStore(tmp_path / "evidence.db")


def make_fetch(tmp_path, html=PAGE, url="https://acme.com/pricing"):
    body = html.encode()
    from tools.archive import archive_bytes
    sha, path = archive_bytes(body, ".html")
    return {"url": url, "final_url": url, "content_type": "text/html", "encoding": "utf-8",
            "method": "http", "fetched_at": "2026-11-02T00:00:00+00:00", "sha256": sha, "archive_path": path}


@pytest.fixture
def source(store, tmp_path):
    return store.ingest(make_fetch(tmp_path))


@pytest.fixture
def acme(store):
    return store.add_entity("Acme", domain="acme.com", domain_status="confirmed")


# --- verify_quote ---------------------------------------------------------------------------

TEXT = "Starter costs $29 per month.\nTeam costs $99 per month, with priority support."


def test_exact_quote_verifies_and_reports_offsets():
    r = verify_quote(TEXT, "Team costs $99 per month, with priority support.")
    assert r["ok"] and normalize(TEXT)[r["start"]:r["end"]] == "Team costs $99 per month, with priority support."


def test_whitespace_differences_are_tolerated():
    assert verify_quote(TEXT, "Starter   costs $29\nper month.")["ok"]


@pytest.mark.parametrize("quote", [
    "Starter costs $39 per month.",           # one digit changed
    "starter costs $29 per month.",           # case changed
    "Starter costs $29 per month!",           # punctuation changed
    "Starter costs about $29 monthly.",       # paraphrase
])
def test_altered_quotes_are_rejected(quote):
    assert not verify_quote(TEXT, quote)["ok"]


def test_short_quotes_are_rejected():
    assert not verify_quote(TEXT, "$29")["ok"]


def test_failed_check_points_at_closest_passage():
    r = verify_quote(TEXT, "Starter costs $39 per month.")
    assert not r["ok"] and "$29" in r["closest_passage"]


# --- sources --------------------------------------------------------------------------------

def test_ingest_is_idempotent_but_records_every_fetch(store, tmp_path):
    fetch = make_fetch(tmp_path)
    a, b = store.ingest(fetch), store.ingest(fetch)
    counts = store.counts()
    assert a == b and counts["sources"] == 1 and counts["fetches"] == 2


def test_ingest_detects_tampered_archive(store, tmp_path):
    fetch = make_fetch(tmp_path)
    Path(fetch["archive_path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        store.ingest(fetch)


# --- claims ---------------------------------------------------------------------------------

def test_verified_claim_is_stored_with_primary_tier(store, source, acme):
    r = store.add_claim(acme, "pricing.starter_monthly_usd", "$29", "Starter costs $29/month.", source, QUOTE)
    assert r["ok"] and r["source_tier"] == "primary"
    [claim] = store.claims_for("acme")
    assert claim["quote"] == QUOTE and claim["source_url"] == "https://acme.com/pricing"


def test_claim_from_other_domain_is_secondary(store, tmp_path, acme):
    src = store.ingest(make_fetch(tmp_path, url="https://reviews.example/acme"))
    r = store.add_claim(acme, "pricing.starter_monthly_usd", "$29", "Starter costs $29.", src, QUOTE)
    assert r["ok"] and r["source_tier"] == "secondary"


def test_unconfirmed_domain_never_grants_primary(store, source):
    ent = store.add_entity("Acme2", domain="acme.com", domain_status="ambiguous")
    assert store.add_claim(ent, "p", "$29", "c $29", source, QUOTE)["source_tier"] == "secondary"


def test_invented_quote_is_rejected_and_logged(store, source, acme):
    r = store.add_claim(acme, "pricing.starter_monthly_usd", "$39", "Starter costs $39.", source,
                        "The Starter plan costs $39 per month, billed annually.")
    assert not r["ok"] and "$29" in r["closest_passage"]
    assert store.counts()["claims"] == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT reason FROM rejected_claims").fetchone()[0].startswith("quote not found")


def test_value_must_be_supported_by_quote_or_claim(store, source, acme):
    r = store.add_claim(acme, "p", "$500", "Starter is pricey.", source, QUOTE)
    assert not r["ok"] and "value" in r["reason"]


def test_unknown_source_is_rejected(store, acme):
    assert not store.add_claim(acme, "p", "$29", "c $29", "0" * 64, QUOTE)["ok"]


def test_tampered_text_file_blocks_claims_and_fails_audit(store, source, acme):
    assert store.add_claim(acme, "p", "$29", "c $29", source, QUOTE)["ok"]
    text_path = store.get_source(source)["text_path"]
    Path(text_path).write_text("something else entirely, with no pricing at all in it", encoding="utf-8")
    assert not store.add_claim(acme, "p2", "$29", "c $29", source, QUOTE)["ok"]
    assert any("no longer matches its hash" in p for p in store.audit())


def test_audit_is_clean_for_untouched_store(store, source, acme):
    store.add_claim(acme, "p", "$29", "c $29", source, QUOTE)
    assert store.audit() == []


# --- append-only ----------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE claims SET value = '$1'",
    "DELETE FROM claims",
    "UPDATE sources SET url = 'x'",
    "DELETE FROM rejected_claims",
    "UPDATE entities SET domain = 'evil.com'",
])
def test_tables_are_append_only(store, source, acme, sql):
    store.add_claim(acme, "p", "$29", "c $29", source, QUOTE)
    store.add_claim(acme, "p", "$39", "bad", source, "invented quote that is not in the page")
    db = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute(sql)
    db.close()


def test_retraction_hides_claim_but_history_stays(store, source, acme):
    cid = store.add_claim(acme, "p", "$29", "c $29", source, QUOTE)["claim_id"]
    store.retract(cid, "page was a draft")
    assert store.claims_for("acme") == []
    assert len(store.claims_for("acme", include_inactive=True)) == 1


def test_supersession_and_contradiction(store, tmp_path, acme):
    old = store.ingest(make_fetch(tmp_path))
    new_html = PAGE.replace("$29", "$35")
    new = store.ingest(make_fetch(tmp_path, html=new_html))
    c_old = store.add_claim(acme, "p", "$29", "c $29", old, QUOTE)["claim_id"]
    c_new = store.add_claim(acme, "p", "$35", "c $35", new, QUOTE.replace("$29", "$35"))["claim_id"]
    store.flag_contradiction(c_new, c_old, "same page, different price over time")
    store.supersede(c_old, c_new, "price changed")
    assert [c["claim_id"] for c in store.claims_for("acme")] == [c_new]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT claim_a, claim_b FROM contradictions").fetchone() == (c_old, c_new)


def test_entities_are_immutable_and_deduplicated(store):
    a = store.add_entity("Notion", domain="notion.com")
    b = store.add_entity("Notion", domain="NOTION.com")
    c = store.add_entity("Notion", domain="notion.sourceforge.net")
    assert a == b and a != c


def test_domain_urls_are_normalized_to_hosts(store):
    assert store.add_entity("Acme", domain="https://www.acme.com/pricing") == store.add_entity("Acme", domain="acme.com")


def test_resolution_registers_status_set_by_code_and_confirmed_wins(store, tmp_path):
    resolution = {"company": "Notion", "candidates": [
        {"domain": "notion.com", "status": "confirmed", "evidence": ["wikidata Q1"]},
        {"domain": "notion.sourceforge.net", "status": "needs_review", "evidence": ["wikidata Q2"]},
    ]}
    store.register_resolution(resolution)
    store.add_entity("Notion", domain="later-guess.example")  # a model-supplied domain: no status
    assert store.find_entity("notion")["domain"] == "notion.com"
    src = store.ingest(make_fetch(tmp_path, url="https://www.notion.com/pricing"))
    claim = store.add_claim(store.find_entity("Notion")["entity_id"], "p", "$29", "c $29", src, QUOTE)
    assert claim["source_tier"] == "primary"


# --- claim support check --------------------------------------------------------------------

from support import SupportVerdict, check_claim, claim_tokens, missing_tokens  # noqa: E402


class FakeLLM:
    model = "fake"

    def __init__(self, verdict, reasoning="r", unsupported=()):
        self.result = SupportVerdict(verdict=verdict, reasoning=reasoning, unsupported_parts=list(unsupported))
        self.prompts = []

    def with_structured_output(self, schema):
        outer = self

        class Runner:
            def invoke(self, prompt):
                outer.prompts.append(prompt)
                return outer.result
        return Runner()


def test_claim_tokens_find_numbers_and_identifiers():
    toks = claim_tokens("WO2021037851A1 lists a $29 plan, 15% off", "$29")
    assert {"WO2021037851A1", "$29", "15%"} <= set(toks)


def test_missing_tokens_flags_numbers_not_in_quote():
    assert missing_tokens("Starter costs $29", "$29", QUOTE) == []
    assert missing_tokens("Starter costs $35", "$35", QUOTE) == ["$35"]


def test_tokens_may_be_traced_to_the_source_url():
    assert missing_tokens("Assignee of WO2021037851A1 is X", "X", "assignee: X",
                          "https://patents.google.com/patent/WO2021037851A1/en") == []
    assert missing_tokens("Assignee of WO2021037851A1 is X", "X", "assignee: X", "https://example.com/") == ["WO2021037851A1"]


def test_support_check_records_verdict_and_gives_model_only_the_passage(store, source, acme):
    cid = store.add_claim(acme, "p", "$29", "Starter costs $29 per month.", source, QUOTE)["claim_id"]
    llm = FakeLLM("supported", "States the price directly.")
    out = check_claim(store, cid, llm=llm)
    assert out["verdict"] == "supported" and store.support_checks_for(cid)[0]["checker"] == "fake"
    assert "Starter plan costs $29" in llm.prompts[0] and "Team plan costs $99" in llm.prompts[0]  # context window


def test_code_downgrades_supported_when_claim_has_tokens_missing_from_quote(store, source, acme):
    cid = store.add_claim(acme, "p", "$29", "Starter costs $29 per month, 15% below Team.", source, QUOTE)["claim_id"]
    out = check_claim(store, cid, llm=FakeLLM("supported"))
    assert out["verdict"] == "partially_supported" and "15%" in out["missing_tokens"]


def test_model_not_supported_verdict_stands_and_checks_are_append_only(store, source, acme):
    cid = store.add_claim(acme, "p", "$29", "Acme is the market leader. $29", source, QUOTE)["claim_id"]
    check_claim(store, cid, llm=FakeLLM("not_supported", "Nothing about market position.", ["market leader"]))
    check_claim(store, cid, llm=FakeLLM("partially_supported"))  # a second opinion adds a row, never replaces
    assert [c["verdict"] for c in store.support_checks_for(cid)] == ["not_supported", "partially_supported"]
    db = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE support_checks SET verdict = 'supported'")
    db.close()


# --- widening a short but verbatim quote (a key and value in an API response) ---

from evidence import widen_quote  # noqa: E402
from tools.archive import archive_bytes  # noqa: E402

JSON_TEXT = '{"cik":"0000320193","name":"Apple Inc.","tickers":["AAPL"],"exchanges":["Nasdaq"],"fiscalYearEnd":"0926","state":"CA","name":"Other Co"}'


def test_a_short_unique_quote_is_widened_with_its_own_neighbouring_text():
    wider = widen_quote(JSON_TEXT, '"tickers":["AAPL"]')
    assert wider is not None and '"tickers":["AAPL"]' in wider and len(wider) >= 20
    assert wider in normalize(JSON_TEXT)                         # still word for word in the source
    assert verify_quote(JSON_TEXT, wider)["ok"]


def test_widening_never_guesses():
    assert widen_quote(JSON_TEXT, "DIS") is None                  # too thin to widen
    assert widen_quote(JSON_TEXT, '"name":"Apple Inc."') is not None
    assert widen_quote(JSON_TEXT + JSON_TEXT, '"tickers":["AAPL"]') is None                 # twice in the source: ambiguous
    assert widen_quote(JSON_TEXT, '"tickers":["MSFT"]') is None                             # not in the source at all
    assert widen_quote(JSON_TEXT, '"fiscalYearEnd":"0926"') is None                        # already long enough: left alone
    assert widen_quote("short", "short text here") is None


def test_the_widened_quote_is_what_the_store_checks_so_a_wrong_claim_still_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.archive.ARCHIVE_DIR", tmp_path / "archive")
    store = EvidenceStore(tmp_path / "e.db")
    sha, path = archive_bytes(JSON_TEXT.encode(), ".json")
    src = store.ingest({"url": "https://data.sec.gov/x.json", "final_url": "https://data.sec.gov/x.json", "content_type": "application/json",
                        "encoding": "utf-8", "method": "api", "fetched_at": "2026-11-04T00:00:00+00:00", "sha256": sha, "archive_path": path})
    eid = store.add_entity("Apple")
    assert store.add_claim(eid, "p", "", "The ticker is AAPL.", src, '"tickers":["AAPL"]')["ok"] is False        # the raw short quote is still refused
    wider = widen_quote(JSON_TEXT, '"tickers":["AAPL"]')
    assert store.add_claim(eid, "p", "", "The ticker is AAPL.", src, wider)["ok"] is True
