"""Figures in prose checked against the financial statements (skills/statements/figures.py): the statements as an index."""

from decimal import Decimal

import pytest

from skills.statements import figures
from skills.temporal.periods import find_periods


def F(concept, printed, value, end, months, unit="USD", decimals=0, verdict="verified", start=None, kind=None,
      statement="income", basis=None, proposed=None, amount=None):
    """One statement value as EvidenceStore.statement_figures returns it."""
    return {"value_id": abs(hash((concept, printed, end, months))) % 10_000, "concept": concept, "printed": printed,
            "amount": amount or printed.replace(",", "").strip("()"), "decimals": decimals, "value": value, "unit": unit,
            "period_kind": kind or ("instant" if months is None else "duration"), "period_start": start, "period_end": end,
            "period_months": months, "verdict": verdict, "proposed": proposed, "statement": statement, "basis": basis,
            "title": "", "source_sha256": "s", "source_url": "https://example.test/q3.htm"}


# A US company with a calendar year (a 10-Q: three and nine months, the nine months starting 1 January)
US = [F("revenue", "3,678", "3678000000", "2026-09-30", 3), F("revenue", "3,443", "3443000000", "2025-09-30", 3),
      F("revenue", "11,020", "11020000000", "2026-09-30", 9, start="2026-01-01"),
      F("net_income_parent", "1,240", "1240000000", "2026-09-30", 3), F("net_income_parent", "1,190", "1190000000", "2025-09-30", 3),
      F("eps_diluted", "4.25", "4.25", "2026-09-30", 3, unit="per_share", decimals=2),
      F("total_assets", "48,032", "48032000000", "2026-09-30", None, statement="balance"),
      F("total_assets", "47,100", "47100000000", "2025-12-31", None, statement="balance")]

# An Indian company (financial year April to March), in crores, with a half year and the year before
IN = [F("revenue", "4,767", "47670000000", "2025-09-30", 3, unit="INR"), F("revenue", "4,533", "45330000000", "2024-09-30", 3, unit="INR"),
      F("revenue", "21,778", "217780000000", "2025-03-31", 12, unit="INR"),
      F("net_income", "317", "3170000000", "2025-09-30", 3, unit="INR"), F("net_income", "273", "2730000000", "2024-09-30", 3, unit="INR"),
      F("revenue", "7,316.94", "731694000", "2023-09-30", 3, unit="INR", decimals=2, verdict="doubt",
        proposed={"reading": "7,316.94", "printed": "7.316.94", "changed": True, "kind": "repair"})]


def one(text, index, context=""):
    [r] = figures.check(text, index, context)
    return r


@pytest.mark.parametrize("text", [
    "Revenue was $3.68 billion in the third quarter of 2026.",
    "Revenue for the three months ended September 30, 2026 was $3,678 million.",
    "The company reported $3.7 billion in revenue for Q3 2026.",
])
def test_a_figure_that_equals_the_statement_at_its_written_precision_matches(text):
    r = one(text, US)
    assert r["verdict"] == "matches" and r["compared_with"][0]["printed"] == "3,678"


def test_a_wrong_figure_for_the_same_line_and_period_differs_and_says_what_the_statement_gives():
    r = one("Revenue was $3.9 billion in the third quarter of 2026.", US)
    assert r["verdict"] == "differs" and "3,678" in r["why"] and "$3.9 billion" in r["why"]


def test_the_quarter_is_placed_from_the_companys_own_year_and_says_how():
    assert figures.fiscal_year_end(US) == 12                          # nine months from 1 January: a calendar year
    assert figures.fiscal_year_end(IN) == 3                           # twelve months to 31 March
    r = one("Net profit was Rs 317 crore in Q2 FY26.", IN)
    assert r["verdict"] == "matches" and "month 3" in r["placed_by"][0]


def test_a_period_that_cannot_be_placed_is_not_guessed():
    only_quarters = [f for f in US if f["period_months"] == 3]
    r = one("Revenue was $3.68 billion in Q3 2026.", only_quarters)
    assert r["verdict"] == "found" and "cannot be placed" in r["why"]     # it says which figure equals it, not that it matches


def test_a_figure_with_no_period_is_looked_up_in_the_index():
    r = one("S&P Global's revenue was $3,678 million.", US)
    assert r["verdict"] == "found" and "3 months ended 2026-09-30" in r["why"]
    assert one("Revenue was $5.1 billion.", US)["verdict"] == "not_found"


def test_each_figure_gets_its_own_period_and_against_carries_the_line_item():
    a, b = figures.check("Net profit was Rs 317 crore in Q2 FY26 against Rs 273 crore in Q2 FY25.", IN)
    assert (a["verdict"], b["verdict"]) == ("matches", "matches")
    assert b["mention"]["phrase"] == "net profit" and b["mention"]["periods"] == ["FY2025 Q2"]
    a, b = figures.check("Revenue was ₹4,767 crore in Q2 FY26, compared with ₹4,533 crore a year earlier.", IN)
    assert b["verdict"] == "matches" and b["mention"]["periods"] == ["FY2025 Q2"]


def test_a_percentage_change_is_checked_allowing_for_the_rounding_of_the_printed_figures():
    # 317 against 273 gives +16.1%, but rounded figures allow 15.7% to 16.5%; the company's own table prints 16.5%
    a, b = figures.check("Net profit was Rs 317 crore for the quarter ended September 30, 2025, up 16.5% year-on-year.", IN)
    assert b["mention"]["periods"] and b["verdict"] == "matches" and "allowing for their rounding" in b["why"]
    r = figures.check("Revenue grew 9% to $3,678 million in the third quarter of 2026.", US)
    assert [x["verdict"] for x in r] == ["differs", "matches"] and "+6.8%" in r[0]["why"]


def test_a_figure_equal_only_to_a_doubtful_statement_figure_is_labelled_for_a_double_check():
    r = one("Revenue was ₹73.17 crore in the quarter ended September 30, 2023.", IN)
    assert r["verdict"] == "matches_doubtful"
    assert "taken as 7,316.94 (printed '7.316.94')" in r["why"] and "please double-check" in r["why"]
    assert figures.summary([r])["label"].startswith("matches only a statement figure in doubt")


@pytest.mark.parametrize("text, why", [
    ("Adjusted revenue was $3.7 billion in Q3 2026.", "different measure"),
    ("Segment revenue was $3.68 billion in the third quarter of 2026.", "different measure"),
    ("The company expects revenue of $15 billion for full year 2026.", "future"),
    ("Revenue was ₹3,678 million in the third quarter of 2026.", "INR"),
])
def test_other_measures_forecasts_and_other_currencies_are_not_compared(text, why):
    r = one(text, US)
    assert r["verdict"] == "not_compared" and why in r["why"]


def test_balance_figures_are_checked_at_the_date_the_sentence_states():
    assert one("Total assets were $48.0 billion as of September 30, 2026.", US)["verdict"] == "matches"
    assert one("Total assets were $60.1 billion as of September 30, 2026.", US)["verdict"] == "differs"


def test_net_income_may_mean_the_parents_share_and_per_share_figures_compare_as_written():
    assert one("Net income was $1.24 billion in the third quarter of 2026.", US)["verdict"] == "matches"
    assert one("Diluted EPS was $4.25 in Q3 2026.", US)["verdict"] == "matches"
    assert one("Diluted EPS was $4.73 in Q3 2026.", US)["verdict"] == "differs"


def test_a_period_for_which_the_statements_give_nothing_is_not_covered():
    r = one("Revenue was $3.1 billion in the third quarter of 2024.", US)
    assert r["verdict"] == "not_covered"


def test_numbers_that_are_not_reported_figures_are_not_read():
    assert figures.check("In 2026 the company opened 12 stores and hired 3,000 people.", US) == []
    assert figures.check("Gross margin was 41%.", US) == []                 # a percentage that is not a change


def test_the_period_may_come_from_the_text_around_the_quote_and_says_so():
    r = one("Revenue was $3,678 million.", US, context="Results for the three months ended September 30, 2026")
    assert r["verdict"] == "matches" and r["mention"]["period_origin"] == "context"


def test_quarter_ended_is_a_period():
    [p] = find_periods("for the quarter ended September 30, 2025")
    assert (p.months, p.end) == (3, "2025-09-30")


def test_a_year_earlier_is_the_same_period_one_year_back():
    [p] = find_periods("Q2 FY26")
    assert figures.year_before(p).label == "FY2025 Q2"


# --- in the store and the run --------------------------------------------------------------------------------------------

def test_statement_sources_index_a_company_and_figure_checks_are_recorded(tmp_path):
    from evidence import EvidenceStore
    from skills import statements
    from tests.test_statements import RELEASE, _ingest
    store = EvidenceStore(tmp_path / "e.db")
    sha = _ingest(store, RELEASE, "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm", "text/html")
    statements.read_source(store, sha)
    entity = store.add_entity("Acme Corp", kind="subject")
    assert store.statement_figures(entity_id=entity) == []                 # not linked yet: not this company's index
    store.link_source_entity(sha, entity)
    store.link_source_entity(sha, entity)                                  # once only
    index = store.statement_figures(entity_id=entity)
    assert index and all(f["concept"] for f in index)
    r = figures.check("Revenue was $1.0 billion.", index)
    from pathlib import Path
    from evidence import normalize
    text = normalize(Path(store.get_source(sha)["text_path"]).read_text(encoding="utf-8"))
    claim = store.add_claim(entity, "financials.revenue", "", "Acme's revenue was reported in its release.", sha, text[:60], created_by="test")
    assert claim["ok"]
    store.record_claim_figures(claim["claim_id"], r, figures.VERSION)
    [stored] = store.claim_figures_for(claim["claim_id"])
    assert stored["verdict"] == r[0]["verdict"] and stored["written"] == "$1.0 billion"
