'''Pure helpers: JSON parsing, citation checks, assumption normalising, text classification.'''

import pytest

import agent

FACTS = [{"id": "F1", "label": "Share price", "value": 230.5, "unit": "$", "period": "latest"},
         {"id": "F2", "label": "P/E ratio", "value": 30.0, "unit": "x", "period": "TTM"}]
RESULTS = [{"id": "S1", "sentiment": "bearish"}, {"id": "S2", "sentiment": "bullish"},
           {"id": "S3", "sentiment": "irrelevant"}]


@pytest.mark.parametrize("reply", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here you go: {"a": 1} hope it helps'])
def test_parse_json_handles_fences_and_chatter(reply):
    assert agent.parse_json(reply) == {"a": 1}


def test_parse_json_without_object_raises():
    with pytest.raises(ValueError):
        agent.parse_json("no json here")


def test_call_agent_json_retries_bad_json(llm):
    replies = iter(["not json", '{"ok": true}'])
    llm.responses["x"] = lambda prompt: next(replies)
    assert agent.call_agent_json("p", role="x") == {"ok": True}
    assert "not valid JSON" in llm.calls[1]["prompt"]


def test_call_agent_json_rejects_non_object(llm):
    llm.responses["x"] = "[1, 2]"
    with pytest.raises(ValueError):
        agent.call_agent_json("p", role="x")


@pytest.mark.parametrize("value, expected", [(None, []), ("a", ["a"]), ({"a": 1}, [{"a": 1}]), ([1], [1]), (3.5, [])])
def test_as_list(value, expected):
    assert agent.as_list(value) == expected


def test_check_citations_normalises_ids_and_drops_unknown():
    items = [{"point": "a", "sources": ["[S1]", "s2", "S9", "S1"]},
             {"point": "b", "sources": "S1, F2"},
             {"point": "c", "sources": [2]},
             {"point": "d", "sources": ["S3"]},   # irrelevant sources can't be cited
             "a bare string point",
             42]
    out = agent.check_citations(items, RESULTS, facts=FACTS)
    assert [o["sources"] for o in out] == [["S1", "S2"], ["S1", "F2"], ["S2"], [], []]
    assert [o["unverified"] for o in out] == [False, False, False, True, True]


def test_check_citations_f_ids_need_facts():
    out = agent.check_citations([{"point": "P/E 30", "sources": ["F2"]}], RESULTS)
    assert out[0]["sources"] == [] and out[0]["unverified"]


def test_check_citations_number_check():
    items = [{"point": "P/E of 30 is high", "sources": ["F2"]},
             {"point": "P/E of 45 is high", "sources": ["F2"]},
             {"point": "Valuation is high", "sources": ["F2"]},
             {"point": "Sales fell 40%", "sources": ["S1"]}]
    out = agent.check_citations(items, RESULTS, facts=FACTS)
    assert [o["numberCheck"] for o in out] == ["verified", "mismatch", None, None]


def test_make_assumptions_normalises_and_caps():
    raw = [{"text": "  iPhone   sales falling "}, "Services growing", {"assumption": "AI helps", "query": "Apple AI"},
           {"text": ""}, None, {"text": "Fourth one"}]
    out = agent.make_assumptions("AAPL", raw)
    assert [a["id"] for a in out] == ["A1", "A2", "A3"]
    assert out[0] == {"id": "A1", "text": "iPhone sales falling", "query": "AAPL iPhone sales falling"}
    assert out[2]["query"] == "Apple AI"


def test_make_assumptions_handles_garbage():
    assert agent.make_assumptions("AAPL", None) == []
    assert agent.make_assumptions("AAPL", "one claim")[0]["text"] == "one claim"


def test_check_assumptions_merges_and_fills_missing():
    assumptions = agent.make_assumptions("AAPL", ["a", "b", "c"])
    reviewed = [{"id": "[A1]", "status": "Supported", "note": "yes", "sources": ["S1"]},
                {"id": "a2", "status": "false", "note": "no", "sources": "S2"},
                {"id": "A2", "status": "supported"},           # duplicate id: first one wins
                {"id": "nonsense", "status": "supported"}]
    out = agent.check_assumptions(assumptions, reviewed, RESULTS)
    assert [a["status"] for a in out] == ["supported", "contradicted", "no_evidence"]
    assert out[2]["note"] and out[2]["unverified"]
    assert out[1]["sources"] == ["S2"]


@pytest.mark.parametrize("status, expected", [("no evidence", "no_evidence"), ("No_Evidence", "no_evidence"),
                                              ("partially supported", "mixed"), ("maybe?", "no_evidence")])
def test_check_assumptions_status_synonyms(status, expected):
    assumptions = agent.make_assumptions("AAPL", ["a"])
    assert agent.check_assumptions(assumptions, [{"id": "A1", "status": status}], RESULTS)[0]["status"] == expected


def test_classify_text():
    assert agent.classify_text(None) is None
    assert agent.classify_text("short") is None
    assert agent.classify_text("Subscribe to continue reading. " + "x" * 400) == "teaser"
    assert agent.classify_text("Plain article text. " * 100) == "article"


def test_next_source_id():
    assert agent.next_source_id([]) == "S1"
    assert agent.next_source_id([{"id": "S1"}, {"id": "S7"}, {"id": "F2"}]) == "S8"


def test_format_sources_hides_irrelevant_and_marks_search():
    results = [{"id": "S1", "date": "2026-10-30", "source": "Yahoo", "sentiment": "bearish", "headline": "H1", "text": "t"},
               {"id": "S2", "date": None, "source": "web", "sentiment": "bullish", "headline": "H2", "summary": "s",
                "origin": "search", "query": "Apple iPhone"},
               {"id": "S3", "date": "2026-10-30", "source": "Yahoo", "sentiment": "irrelevant", "headline": "H3", "text": "t"}]
    text = agent.format_sources(results)
    assert "S1:" in text and "S3:" not in text
    assert 'web search: "Apple iPhone"' in text and "undated" in text
    assert agent.format_sources([]) == "(no news sources)"
