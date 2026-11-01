'''/checkin: revisiting a past thesis against the news published since.'''

import pytest

import agent
import db
import user
from conftest import NOW


def run(stock="apple", reason="negative press on iPhone sales"):
    return agent.run_analysis("u1", stock, reason=reason, stock_risk="medium", general_risk="low",
                              intent="signal", n=3)


def new_news(world):
    '''Fresh Finnhub articles after the analysis; the news window is expired so they get fetched.'''
    world.news = [{"id": 100 + i, "headline": f"Fresh AAPL story {i}", "summary": "s", "source": "Reuters",
                   "url": f"https://news.example/fresh/{i}", "datetime": NOW + 60 + i} for i in range(2)] + world.news
    db.news_fetches.delete_many({})


def test_checkin_uses_only_news_the_user_has_not_seen(llm, world):
    out = run()
    seen = {s["url"] for s in out["sources"]}
    new_news(world)
    [c] = agent.thesis_checkin("u1")
    urls = [s["url"] for s in c["sources"]]
    assert urls and not set(urls) & seen
    assert "https://news.example/fresh/0" in urls
    assert [s["id"] for s in c["sources"]] == [f"S{i + 1}" for i in range(len(urls))]
    prompt = llm.prompts("checkin")[0]
    assert "negative press on iPhone sales" in prompt and "then: supported" in prompt
    assert c["assumptions"][0]["change"] == "weaker" and c["assumptions"][0]["sources"] == ["S1"]
    assert db.checkins.count_documents({"userId": "u1"}) == 1


def test_checkin_without_new_news_skips_llm(llm):
    run()
    [c] = agent.thesis_checkin("u1")
    assert "checkin" not in llm.roles()
    assert c["assumptions"][0]["change"] == "no_news" and "No new news" in c["summary"]


def test_checkin_shows_price_move(world):
    run()
    world.quote = {"c": 253.55, "dp": 1.0}
    db.financials.delete_many({})
    [c] = agent.thesis_checkin("u1")
    assert c["priceThen"] == 230.5 and c["priceNow"] == 253.55


def test_checkin_latest_thesis_per_stock(llm, world):
    run(reason="first idea")
    run(reason="second idea")
    run(stock="msft", reason="cloud")
    checkins = agent.thesis_checkin("u1")
    assert [c["ticker"] for c in checkins] == ["MSFT", "AAPL"]
    assert checkins[1]["reason"] == "second idea"
    [only] = agent.thesis_checkin("u1", "apple")
    assert only["ticker"] == "AAPL"


def test_checkin_old_thesis_without_queries(llm, world):
    run()
    db.theses.update_one({}, {"$set": {"assumptions": []}})
    new_news(world)
    [c] = agent.thesis_checkin("u1")
    assert c["assumptions"][0]["text"] == "negative press on iPhone sales"


def test_checkin_with_no_theses():
    with pytest.raises(ValueError, match="No past analyses"):
        agent.thesis_checkin("u1")


def test_checkin_with_memory_off():
    run()
    user.set_memory("u1", False)
    with pytest.raises(ValueError, match="Memory is off"):
        agent.thesis_checkin("u1")


def test_checkin_for_a_stock_never_analysed():
    run()
    with pytest.raises(ValueError, match="No past analysis of MSFT"):
        agent.thesis_checkin("u1", "msft")


def test_checkin_ignores_memory_off_runs():
    user.set_memory("u1", False)
    run()
    user.set_memory("u1", True)
    with pytest.raises(ValueError, match="No past analyses"):
        agent.thesis_checkin("u1")


def test_checkin_bad_llm_reply_defaults(llm, world):
    run()
    new_news(world)
    llm.responses["checkin"] = {"assumptions": "garbage", "summary": None}
    [c] = agent.thesis_checkin("u1")
    assert c["assumptions"][0]["change"] == "no_news" and c["summary"] == ""


def test_checkin_deleted_with_thesis_and_forget(world):
    run()
    agent.thesis_checkin("u1")
    user.delete_thesis("u1", db.theses.find_one({})["_id"])
    assert db.checkins.count_documents({}) == 0
    run()
    agent.thesis_checkin("u1")
    assert user.forget_user("u1")["checkins"] == 1


def test_repeat_checkin_does_not_show_the_same_news_again(llm, world):
    run()
    new_news(world)
    [first] = agent.thesis_checkin("u1")
    assert first["sources"]
    db.news_fetches.delete_many({})
    [second] = agent.thesis_checkin("u1")
    assert not {s["url"] for s in first["sources"]} & {s["url"] for s in second["sources"]}
    assert second["assumptions"][0]["change"] == "no_news"


def test_checkin_not_saved_if_thesis_forgotten_meanwhile(llm, world, monkeypatch):
    run()
    new_news(world)
    real = agent.label_sentiments

    def forget_midway(*a, **k):
        user.forget_user("u1")
        return real(*a, **k)
    monkeypatch.setattr(agent, "label_sentiments", forget_midway)
    assert agent.thesis_checkin("u1") == []
    assert db.checkins.count_documents({}) == 0


def test_checkin_not_saved_if_memory_turned_off_meanwhile(llm, world, monkeypatch):
    run()
    new_news(world)
    real = agent.label_sentiments

    def off_midway(*a, **k):
        user.set_memory("u1", False)
        return real(*a, **k)
    monkeypatch.setattr(agent, "label_sentiments", off_midway)
    assert agent.thesis_checkin("u1") == []
    assert db.checkins.count_documents({}) == 0


def test_one_failing_stock_does_not_lose_the_others(llm, world, monkeypatch):
    run()
    run(stock="msft", reason="cloud")
    real = agent.gather_top_n_news

    def flaky(stock, *a, **k):
        if stock == "MSFT":
            raise RuntimeError("finnhub 429")
        return real(stock, *a, **k)
    monkeypatch.setattr(agent, "gather_top_n_news", flaky)
    assert [c["ticker"] for c in agent.thesis_checkin("u1")] == ["AAPL"]
    monkeypatch.setattr(agent, "gather_top_n_news", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        agent.thesis_checkin("u1")
