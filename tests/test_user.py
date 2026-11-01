'''User pipeline: profile, theses, responses, blind spots, memory on/off, similar stocks, deleting.'''

from datetime import timedelta

import pytest
from bson import ObjectId

import db
import user


def thesis(user_id="u1", ticker="AAPL", reason="iPhone sales", peers=(), remembered=True, assumptions=None,
           days_ago=0):
    session_id = db.sessions.insert_one({"userId": user_id, "ticker": ticker}).inserted_id
    t = user.save_thesis(user_id, session_id, ticker.lower(), reason, "signal", "medium", ticker,
                         assumptions=assumptions, peers=peers, remembered=remembered)
    if days_ago:
        db.theses.update_one({"_id": t["_id"]}, {"$set": {"createdAt": db.now() - timedelta(days=days_ago)}})
    return t


def objection(user_id="u1", category="valuation", response=None, remembered=True, session_id=None):
    oid = db.objections.insert_one({"userId": user_id, "sessionId": session_id or ObjectId(), "ticker": "AAPL",
                                    "category": category, "claim": "c", "response": None,
                                    "remembered": remembered}).inserted_id
    if response:
        user.record_response(user_id, oid, response)
    return oid


# ---------- Profile ----------

def test_upsert_user_defaults_and_validation():
    profile = user.upsert_user("u1", "HIGH", name="Sam")
    assert profile["generalRisk"] == "high" and profile["memoryOn"] is True and profile["mutedCategories"] == []
    with pytest.raises(ValueError):
        user.upsert_user("u1", "extreme")


def test_upsert_keeps_memory_setting():
    user.set_memory("u1", False)
    assert user.upsert_user("u1", "low")["memoryOn"] is False
    assert user.memory_on("u1") is False


def test_memory_on_by_default_for_unknown_user():
    assert user.memory_on("nobody") is True


def test_mute_category():
    user.upsert_user("u1")
    user.mute_category("u1", " Debt ")
    assert user.get_user("u1")["mutedCategories"] == ["debt"]
    user.mute_category("u1", "debt", mute=False)
    assert user.get_user("u1")["mutedCategories"] == []


# ---------- Theses ----------

def test_save_thesis_validates_and_stores_assumptions():
    t = thesis(assumptions=[{"id": "A1", "text": "x", "status": "supported", "note": "n", "query": "q",
                             "sources": ["S1"]}], peers=["MSFT"])
    stored = db.theses.find_one({"_id": t["_id"]})
    assert stored["assumptions"] == [{"id": "A1", "text": "x", "status": "supported", "note": "n"}]
    assert stored["peers"] == ["MSFT"] and stored["remembered"] is True
    with pytest.raises(ValueError):
        user.save_thesis("u1", ObjectId(), "x", intent="gamble")


def test_record_decision():
    t = thesis()
    user.record_decision("u1", str(t["sessionId"]), "less_sure")
    assert db.theses.find_one({"_id": t["_id"]})["decision"] == "less_sure"
    with pytest.raises(ValueError):
        user.record_decision("u2", t["sessionId"], "less_sure")      # someone else's
    with pytest.raises(ValueError):
        user.record_decision("u1", "not-an-id", "less_sure")


def test_past_theses_includes_similar_stocks_both_ways():
    thesis(ticker="AAPL", days_ago=3)
    thesis(ticker="MSFT", days_ago=2)                     # in AAPL's current peers
    thesis(ticker="GOOGL", peers=["AAPL"], days_ago=1)    # AAPL in its stored peers
    thesis(ticker="TSLA")                                 # unrelated
    tickers = [t["ticker"] for t in user.past_theses("u1", "AAPL", peers=["MSFT"])]
    assert tickers == ["GOOGL", "MSFT", "AAPL"]
    assert len(user.past_theses("u1")) == 4


def test_past_theses_excludes_forgotten_and_other_users():
    thesis(remembered=False)
    thesis(user_id="u2")
    assert user.past_theses("u1") == []
    assert len(user.past_theses("u1", include_forgotten=True)) == 1


def test_old_theses_without_flag_count_as_remembered():
    t = thesis()
    db.theses.update_one({"_id": t["_id"]}, {"$unset": {"remembered": ""}})
    assert len(user.past_theses("u1")) == 1


# ---------- Responses and blind spots ----------

def test_record_response_owner_only_and_valid():
    oid = objection()
    with pytest.raises(ValueError):
        user.record_response("u2", oid, "dismiss")
    with pytest.raises(ValueError):
        user.record_response("u1", oid, "ignore")
    with pytest.raises(ValueError):
        user.get_objection("u2", oid)
    user.record_response("u1", oid, "rebut", "no")
    assert user.get_objection("u1", oid)["rebuttal"] == "no"


def test_blind_spots_ranked_and_muted():
    for response in ["dismiss", "dismiss", "accept"]:
        objection(category="valuation", response=response)
    objection(category="debt", response="dismiss")
    objection(category="macro")                            # no response: not counted
    spots = user.get_blind_spots("u1")
    assert list(spots) == ["valuation", "debt"]
    assert spots["valuation"] == {"dismissed": 2, "total": 3, "muted": False}
    user.upsert_user("u1")
    user.mute_category("u1", "valuation")
    assert list(user.get_blind_spots("u1")) == ["debt"]
    assert user.get_blind_spots("u1", include_muted=True)["valuation"]["muted"] is True


def test_blind_spots_ignore_unremembered_runs():
    objection(category="valuation", response="dismiss", remembered=False)
    assert user.get_blind_spots("u1") == {}


# ---------- Memory context ----------

def test_memory_context_empty():
    assert user.memory_context("u1", "AAPL") == "No past history for this user."


def test_memory_context_mentions_similar_stocks_assumptions_and_dismissals():
    t = thesis(ticker="MSFT", reason="Cloud is booming",
               assumptions=[{"id": "A1", "text": "Azure growing", "status": "contradicted"}])
    user.record_decision("u1", t["sessionId"], "dropping")
    objection(category="valuation", response="dismiss")
    text = user.memory_context("u1", "AAPL", peers=["MSFT"])
    assert "MSFT (a similar company)" in text and "Cloud is booming" in text
    assert "decided: dropping" in text
    assert 'assumed "Azure growing": contradicted' in text
    assert "valuation (1/1 dismissed)" in text


def test_memory_context_without_peers_ignores_other_stocks():
    thesis(ticker="MSFT")
    assert user.memory_context("u1", "AAPL") == "No past history for this user."


# ---------- Deleting ----------

def test_delete_thesis_removes_its_run_only():
    keep = thesis(ticker="MSFT")
    drop = thesis(ticker="AAPL")
    objection(session_id=drop["sessionId"], response="dismiss")
    objection(session_id=keep["sessionId"], response="dismiss", category="debt")
    db.messages.insert_one({"sessionId": drop["sessionId"], "content": "x"})
    with pytest.raises(ValueError):
        user.delete_thesis("u2", drop["_id"])             # not theirs
    user.delete_thesis("u1", str(drop["_id"]))
    assert [t["ticker"] for t in user.past_theses("u1")] == ["MSFT"]
    assert list(user.get_blind_spots("u1")) == ["debt"]
    assert db.messages.count_documents({}) == 0
    assert db.sessions.count_documents({"_id": drop["sessionId"]}) == 0
    with pytest.raises(ValueError):
        user.delete_thesis("u1", drop["_id"])


def test_forget_user_keeps_shared_news_and_other_users():
    thesis()
    thesis(user_id="u2")
    objection(response="dismiss")
    user.upsert_user("u1")
    db.news.insert_one({"ticker": "AAPL", "url": "u", "headline": "h"})
    counts = user.forget_user("u1")
    assert counts["theses"] == 1 and counts["users"] == 1 and counts["objections"] == 1
    assert db.news.count_documents({}) == 1
    assert len(user.past_theses("u2")) == 1


def test_get_memory():
    thesis()
    memory = user.get_memory("u1")
    assert memory["memoryOn"] is True and len(memory["theses"]) == 1 and memory["blindSpots"] == {}


def test_get_memory_lists_memory_off_runs_so_they_can_be_deleted():
    t = thesis(remembered=False)
    assert [x["_id"] for x in user.get_memory("u1")["theses"]] == [t["_id"]]
    user.delete_thesis("u1", t["_id"])
    assert user.get_memory("u1")["theses"] == []
