"""The Answers tab: only the answer, numbered citations and their sources, and a Word copy (api/answer.py, /api/runs/{id}/answer)."""

import io
import zipfile
from xml.dom import minidom

from api import answer
from tests.test_run_control import H, api
from tests.test_runner import env  # noqa: F401

REPORT = {
    "question": "On what exact date was 'Attention Is All You Need' first submitted?",
    "subject": "Attention Is All You Need",
    "outcome": "answered",
    "summary": ("The paper was submitted on 12 June 2017 (Web page by arxiv.org, dated 2017-06-12, https://arxiv.org/abs/1706.03762) [claim:2]"
                " [claim:3]. It is widely cited [claim:9].\n\nSources:\n- [claim:2] Tier 3: Web page by arxiv.org, https://arxiv.org/abs/1706.03762.\n"
                "- [claim:9] Tier 3: Reference entry by Wikipedia, https://en.wikipedia.org/wiki/X_(paper)."),
    "established": [
        {"claim_id": 2, "quote": "[v1] Mon, 12 Jun 2017 17:57:34 UTC", "source_url": "https://arxiv.org/abs/1706.03762",
         "source_label": "Web page by arxiv.org", "source_date": "2017-06-12"},
        {"claim_id": 3, "quote": "Submitted on 12 Jun 2017 (v1)", "source_url": "https://arxiv.org/abs/1706.03762",
         "source_label": "Web page by arxiv.org", "source_date": "2017-06-12"},
    ],
    "partial": [{"claim_id": 9, "quote": "It is one of the most cited papers", "source_url": "https://en.wikipedia.org/wiki/X_(paper)",
                 "source_label": "Reference entry by Wikipedia", "source_date": None}],
    "gaps": ["Exact submission time zone"],
    "time": {"today": "2026-11-12", "stale": False},
}


def test_markers_become_one_number_per_source_and_the_writers_source_notes_go():
    v = answer.build(REPORT)
    assert v["answer"] == "The paper was submitted on 12 June 2017 [1]. It is widely cited [2]."
    assert [(s["n"], s["url"], [q["claim_id"] for q in s["quotes"]]) for s in v["sources"]] == [
        (1, "https://arxiv.org/abs/1706.03762", [2, 3]), (2, "https://en.wikipedia.org/wiki/X_(paper)", [9])]
    assert v["sources"][1]["quotes"][0]["caution"] and v["sources"][0]["quotes"][0]["quote"] == "[v1] Mon, 12 Jun 2017 17:57:34 UTC"
    assert "gaps" not in v and any("partly supported" in n for n in v["notes"])
    assert v["as_of"] == "2026-11-12"


def test_what_was_not_found_is_left_out_of_the_answer():
    summary = ("# Deep research report: Acme\n\n## Summary\n\nAcme raised prices [claim:2].\n\n"
               "## What key events has Acme announced in 2026, and when did they occur?\n\nNot established from what was read.\n\n"
               "## What did Acme say?\n\n- “It said so” [claim:3]\n\n"
               "## Who are its partners?\n\nNot established from what was read.\n\n"
               "Not established from what was read: Acme's partners; Acme's 2026 events.\n\n"
               "## Sources\n1. Tier 3: Web page by arxiv.org (claims 2, 3)\n\n## How the research was done\n- Searches: 4.\n\n"
               "## Not established\n- Who are its partners?: nothing was found in what was read")
    v = answer.build({**REPORT, "summary": summary})
    assert v["answer"] == ("# Deep research report: Acme\n\n## Summary\n\nAcme raised prices [1].\n\n"
                           "## What did Acme say?\n\n- “It said so” [1]")
    assert not any("Not established" in n for n in v["notes"])
    brief = ("Acme raised prices [claim:2].\n\nSources:\n- [claim:2] Tier 3: Web page by arxiv.org.\n"
             "Team plan price (not established from what was read).")
    assert answer.build({**REPORT, "summary": brief})["answer"] == "Acme raised prices [1]."


def test_a_marker_for_an_unknown_claim_is_dropped_not_numbered():
    v = answer.build({**REPORT, "summary": "A fact [claim:404].", "established": [], "partial": []})
    assert v["answer"] == "A fact." and v["sources"] == []


def test_the_word_copy_is_a_valid_package_with_the_answer_quotes_and_links():
    v = answer.build(REPORT)
    z = zipfile.ZipFile(io.BytesIO(answer.to_docx(v, run_id="abcdef123456")))
    assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml", "word/_rels/document.xml.rels"} <= set(z.namelist())
    for name in z.namelist():
        minidom.parseString(z.read(name))                                   # every part is well-formed XML
    doc = z.read("word/document.xml").decode()
    assert "12 June 2017" in doc and "[1]" in doc and "Submitted on 12 Jun 2017 (v1)" in doc and "Not established" not in doc
    rels = z.read("word/_rels/document.xml.rels").decode()
    assert 'Target="https://arxiv.org/abs/1706.03762"' in rels and 'TargetMode="External"' in rels
    assert answer.filename(v).endswith(".docx") and " " not in answer.filename(v)


def test_the_api_gives_the_answer_and_the_word_file_only_when_the_run_is_done(env, tmp_path):
    client, jobs, _ = api(env, tmp_path)
    waiting = jobs.start_research("What does Acme charge?", "Acme", {"max_iterations": 1}, False, require_approval=True)
    r = client.get(f"/api/runs/{waiting}/answer", headers=H).json()
    assert r["status"] == "awaiting_approval" and r["answer"] is None
    assert client.get(f"/api/runs/{waiting}/answer.docx", headers=H).status_code == 409
    done = jobs.start_research("What does Acme charge?", "Acme", {"max_iterations": 1}, False, require_approval=False, ask_sources=False)
    r = client.get(f"/api/runs/{done}/answer", headers=H).json()
    assert r["status"] == "finished" and r["answer"]["question"] == "What does Acme charge?"
    assert "[claim:" not in r["answer"]["answer"]
    d = client.get(f"/api/runs/{done}/answer.docx", headers=H)
    assert d.status_code == 200 and d.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="What-does-Acme-charge.docx"' in d.headers["content-disposition"]
    assert client.get("/api/runs/nope/answer", headers=H).status_code == 404
    assert client.get(f"/api/runs/{done}/answer.docx").status_code == 401             # the token is needed, like every /api call
