'''Agents (LLM faked), the real OpenRouter call (HTTP faked), rebuttals with web search, and run_analysis.'''

import json
import types

import pytest

import agent
import db
import user
from conftest import REAL_CALL_AGENT

RESULTS = [{"id": "S1", "sentiment": "bearish", "date": "2026-10-30", "source": "Yahoo", "headline": "h1", "text": "t"},
           {"id": "S2", "sentiment": "bullish", "date": "2026-10-30", "source": "Yahoo", "headline": "h2", "text": "t"}]
FACTS = [{"id": "F1", "label": "P/E ratio", "value": 30.0, "unit": "x", "period": "TTM"}]


# ---------- OpenRouter call ----------

def http(status=200, body=None, text=None, headers=None):
    return types.SimpleNamespace(status_code=status, headers=headers or {},
                                 text=text if text is not None else json.dumps(body),
                                 json=lambda: json.loads(text if text is not None else json.dumps(body)))


def test_call_agent_retries_rate_limits_and_logs(monkeypatch):
    replies = iter([http(429, {}, headers={"Retry-After": "0"}),
                    http(200, {"choices": [{"message": {"content": "hi"}}]})])
    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: next(replies))
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    session_id = db.sessions.insert_one({}).inserted_id
    assert REAL_CALL_AGENT("p", "sys", session_id=session_id, user_id="u1", role="critic") == "hi"
    logged = db.messages.find_one({"sessionId": session_id})
    assert logged["role"] == "critic" and logged["content"] == "hi"


@pytest.mark.parametrize("response", [http(200, {"error": {"message": "bad key"}}), http(200, {"choices": []}),
                                      http(502, text="<html>Bad gateway</html>")])
def test_call_agent_errors(monkeypatch, response):
    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: response)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        REAL_CALL_AGENT("p")


# ---------- Assumptions ----------

def test_extract_assumptions(llm):
    out = agent.extract_assumptions("AAPL", "negative press on iPhone sales", "signal")
    assert out == [{"id": "A1", "text": "iPhone sales are falling", "query": "Apple iPhone sales decline"}]
    assert "negative press on iPhone sales" in llm.prompts("assumptions")[0]


def test_extract_assumptions_skips_empty_reason(llm):
    assert agent.extract_assumptions("AAPL", "  ") == []
    assert llm.calls == []


# ---------- Researcher, critic, devil ----------

def test_researcher_shows_facts_and_checks_numbers(llm):
    out = agent.researcher_summary("AAPL", RESULTS, {"bearish": 1}, FACTS, role="researcher")
    assert "F1: P/E ratio (TTM) = 30.00x" in llm.prompts("researcher")[0]
    point = out["key_points"][0]
    assert point["sources"] == ["S1"]          # F3 isn't a real fact here
    llm.responses["researcher"] = {"summary": "s", "key_points": [{"point": "P/E is 30", "sources": ["F1"]}]}
    assert agent.researcher_summary("AAPL", RESULTS, {}, FACTS, role="researcher")["key_points"][0]["numberCheck"] == "verified"


def test_critic_checks_every_assumption(llm):
    assumptions = agent.make_assumptions("AAPL", ["iPhone sales falling", "Services growing"])
    llm.responses["critic"] = {"assumptions": [{"id": "A1", "status": "supported", "note": "yes", "sources": ["S1"]}],
                               "issues": [{"target": "USER", "issue": "x", "sources": ["S1"]},
                                          {"target": "nobody", "issue": "y"}, {"issue": ""}, "junk"],
                               "verdict": "ok"}
    out = agent.critic("AAPL", RESULTS, {}, "reason", "low", "low", assumptions=assumptions, facts=FACTS, role="critic")
    assert [a["status"] for a in out["assumptions"]] == ["supported", "no_evidence"]
    assert [i["target"] for i in out["issues"]] == ["user", "researcher"]
    prompt = llm.prompts("critic")[0]
    assert "A2: Services growing" in prompt and "no_evidence" in prompt


def test_critic_prompt_says_silence_is_not_falsehood():
    assert "does not make the user wrong" in agent.CRITIC_SYSTEM


def test_devil_normalises_and_ranks_by_blind_spots(llm):
    llm.responses["devil"] = {"objections": [
        {"category": "Valuation", "claim": "a", "sources": ["S1"]},
        {"category": "Debt levels", "claim": "b", "sources": ["S2"]},
        {"category": "weird", "claim": "c", "sources": []},
        {"category": "debt", "claim": ""}]}
    spots = {"debt": {"dismissed": 2, "total": 3, "muted": False}}
    out = agent.devils_advocate("AAPL", RESULTS, {}, "r", "low", "low", spots, facts=FACTS, role="devil")
    assert [o["category"] for o in out] == ["debt", "valuation", "other"]
    assert out[0]["whyFirst"] == "You dismissed 2 of 3 debt points before"
    assert out[2]["unverified"] is True
    assert "debt" in llm.prompts("devil")[0]


def test_devil_handles_null_objections(llm):
    llm.responses["devil"] = {"objections": None}
    assert agent.devils_advocate("AAPL", RESULTS, {}, "r", "low", "low", {}, role="devil") == []


# ---------- Full run ----------

def run(**kwargs):
    args = dict(user_id="u1", stock="apple", reason="negative press on iPhone sales", stock_risk="medium",
                general_risk="low", intent="signal", n=3)
    return agent.run_analysis(**{**args, **kwargs})


def test_run_analysis_end_to_end(llm, world):
    out = run()
    assert out["ticker"] == "AAPL" and out["peers"] == ["MSFT", "GOOGL"]
    assert [a["id"] for a in out["sources"]] == ["S1", "S2", "S3", "S4"]
    assert out["sources"][-1]["origin"] == "search"
    assert out["assumptions"][0]["status"] == "supported"
    assert out["objections"][0]["numberCheck"] == "verified"
    assert llm.roles()[:1] == ["assumptions"]
    session = db.sessions.find_one({"_id": out["sessionId"]})
    assert session["status"] == "done" and session["facts"] and session["assumptions"]
    assert session["sources"][-1]["origin"] == "search"
    thesis = db.theses.find_one({"sessionId": out["sessionId"]})
    assert thesis["assumptions"][0]["status"] == "supported" and thesis["peers"] == ["MSFT", "GOOGL"]
    assert db.objections.count_documents({"sessionId": out["sessionId"]}) == 2
    # The search found news about the user's claim, so the agents saw it
    assert "Apple iPhone sales decline" in llm.prompts("critic")[0]


def test_run_uses_confirmed_assumptions(llm, world):
    out = run(assumptions=["Services revenue is growing"], ticker="AAPL")
    assert "assumptions" not in llm.roles()
    assert out["assumptions"][0]["text"] == "Services revenue is growing"
    assert out["assumptions"][0]["status"] == "supported"


def test_run_without_reason_skips_assumptions_and_search(llm, world):
    out = run(reason=None)
    assert out["assumptions"] == [] and world.counts["search"] == 0
    assert all(a["origin"] == "news" for a in out["sources"])


def test_run_survives_fact_table_outage(monkeypatch, llm):
    import financials
    monkeypatch.setattr(financials, "get_company_basic_financials", lambda *a: (_ for _ in ()).throw(RuntimeError("429")))
    out = run()
    assert out["facts"] == [] and "(no financial data)" in llm.prompts("researcher")[0]


def test_run_survives_search_outage(world):
    world.search_error = RuntimeError("tavily down")
    out = run()
    assert len(out["sources"]) == 3


def test_second_run_sees_similar_stock_memory(llm, world):
    # Forward direction only: the MSFT thesis doesn't list AAPL, but AAPL's peers include MSFT
    world.peers = []
    run(stock="msft", reason="Cloud is booming")
    assert db.theses.find_one({"ticker": "MSFT"})["peers"] == []
    world.peers = ["AAPL", "MSFT"]
    db.companies.delete_many({})
    run()
    assert "MSFT (a similar company)" in llm.prompts("devil")[-1]


def test_similar_stock_memory_reverse_direction(llm, world):
    world.peers = ["MSFT", "AAPL"]
    run(stock="msft", reason="Cloud is booming")             # stored with peers ["AAPL"]
    world.peers = []
    db.companies.delete_many({})
    run()
    assert "MSFT (a similar company)" in llm.prompts("devil")[-1]


def test_memory_turned_off_mid_run_wins(llm):
    def turn_off(prompt):
        user.set_memory("u1", False)
        return {"objections": [{"category": "debt", "claim": "c", "sources": ["S1"]}]}
    llm.responses["devil"] = turn_off
    out = run()
    assert db.theses.find_one({})["remembered"] is False
    assert db.objections.find_one({})["remembered"] is False
    assert db.sessions.find_one({"_id": out["sessionId"]})["remembered"] is False


def test_memory_off_run_is_not_remembered(llm):
    user.set_memory("u1", False)
    first = run()
    assert first["remembered"] is False and first["memory"] is None
    for o in first["objections"]:
        user.record_response("u1", o["_id"], "dismiss")
    assert user.past_theses("u1") == [] and user.get_blind_spots("u1") == {}
    user.set_memory("u1", True)
    second = run()
    assert second["memory"] == "No past history for this user." and second["blindSpots"] == {}


def test_memory_on_dismissals_reorder_next_run(llm):
    first = run()
    debt = next(o for o in first["objections"] if o["category"] == "debt")
    user.record_response("u1", debt["_id"], "dismiss")
    second = run()
    assert second["objections"][0]["category"] == "debt" and second["objections"][0]["whyFirst"]


def test_failed_run_leaves_no_memory(llm):
    llm.responses["devil"] = lambda prompt: (_ for _ in ()).throw(RuntimeError("LLM down"))
    with pytest.raises(RuntimeError):
        run()
    assert db.theses.count_documents({}) == 0 and db.objections.count_documents({}) == 0
    assert db.sessions.find_one({})["status"] == "error"


def test_unknown_company_fails_cleanly():
    with pytest.raises(ValueError):
        run(stock="not a company")
    assert db.sessions.find_one({})["status"] == "error"


def test_forget_during_run_keeps_nothing(llm):
    def forget_then_answer(prompt):
        user.forget_user("u1")
        return {"objections": []}
    llm.responses["devil"] = forget_then_answer
    with pytest.raises(ValueError):
        run()
    for collection in (db.theses, db.objections, db.sessions, db.messages, db.users):
        assert collection.count_documents({}) == 0


# ---------- Rebuttal ----------

def test_rebuttal_searches_claim_and_can_cite_new_source(llm, world):
    out = run()
    objection = out["objections"][0]
    llm.responses["rebuttal"] = lambda prompt: {"verdict": "concede", "reply": "Fair, S5 backs you.",
                                                "sources": ["S5"]}
    result = agent.rebuttal_reply("u1", objection["_id"], "Reuters says iPhone 17 demand is strong")
    assert result["verdict"] == "concede" and result["sources"] == ["S5"]
    assert [s["id"] for s in result["newSources"]] == ["S5"]
    assert "iPhone 17 demand" in llm.prompts("rebuttal")[0] and "S5:" in llm.prompts("rebuttal")[0]
    session = db.sessions.find_one({"_id": out["sessionId"]})
    assert [s["id"] for s in session["sources"]][-1] == "S5"
    stored = user.get_objection("u1", objection["_id"])
    assert stored["response"] == "rebut" and stored["exchanges"][0]["reply"] == "Fair, S5 backs you."


def test_rebuttal_sources_are_labelled_and_irrelevant_hidden(llm, world):
    out = run()
    llm.responses["sentiment"] = {"sentiment": "irrelevant", "reason": "off topic"}
    result = agent.rebuttal_reply("u1", out["objections"][0]["_id"], "something unrelated")
    assert result["newSources"][0]["sentiment"] == "irrelevant"
    assert "S5:" not in llm.prompts("rebuttal")[0]
    stored = db.sessions.find_one({"_id": out["sessionId"]})["sources"][-1]
    assert stored["sentiment"] == "irrelevant"


def test_rebuttal_can_cite_facts(llm):
    out = run()
    llm.responses["rebuttal"] = {"verdict": "hold", "reply": "P/E is 30.0, still rich.", "sources": ["F3"]}
    result = agent.rebuttal_reply("u1", out["objections"][0]["_id"], "it's cheap")
    assert result["sources"] == ["F3"] and result["numberCheck"] == "verified"
    assert "F3: P/E ratio" in llm.prompts("rebuttal")[0]


def test_second_rebuttal_continues_numbering_and_history(llm, world):
    out = run()
    oid = out["objections"][0]["_id"]
    agent.rebuttal_reply("u1", oid, "first point")
    second = agent.rebuttal_reply("u1", oid, "second point")
    assert [s["id"] for s in second["newSources"]] == ["S6"]
    assert "User: first point" in llm.prompts("rebuttal")[1]


def test_rebuttal_search_outage_still_replies(llm, world):
    out = run()
    world.search_error = RuntimeError("down")
    result = agent.rebuttal_reply("u1", out["objections"][0]["_id"], "my claim")
    assert result["newSources"] == [] and result["verdict"] == "hold"


def test_rebuttal_bad_verdict_defaults_to_hold(llm):
    out = run()
    llm.responses["rebuttal"] = {"verdict": "maybe", "reply": ""}
    result = agent.rebuttal_reply("u1", out["objections"][0]["_id"], "x")
    assert result["verdict"] == "hold" and result["reply"]


def test_rebuttal_on_someone_elses_objection():
    out = run()
    with pytest.raises(ValueError):
        agent.rebuttal_reply("u2", out["objections"][0]["_id"], "x")


def test_lookup_stock():
    info = agent.lookup_stock("apple")
    assert info["ticker"] == "AAPL" and info["facts"][0]["label"] == "Share price" and info["peers"]
