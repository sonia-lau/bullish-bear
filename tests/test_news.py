'''News cache, article text, web search results and sentiment labels.'''

from datetime import timedelta

import agent
import db
from conftest import finnhub_news


def test_news_window_is_cached(world):
    agent.fetch_news_to_cache("AAPL", "2026-10-20", "2026-11-01")
    assert agent.fetch_news_to_cache("AAPL", "2026-10-20", "2026-11-01") is False
    assert world.counts["news"] == 1
    db.news_fetches.update_one({}, {"$set": {"fetchedAt": db.now() - timedelta(hours=agent.CACHE_HOURS + 1)}})
    assert agent.fetch_news_to_cache("AAPL", "2026-10-20", "2026-11-01") is True


def test_refetch_keeps_sentiment(world):
    agent.fetch_news_to_cache("AAPL", "2026-10-20", "2026-11-01")
    db.news.update_many({}, {"$set": {"sentiment": "bullish"}})
    db.news_fetches.delete_many({})
    agent.fetch_news_to_cache("AAPL", "2026-10-20", "2026-11-01")
    assert db.news.count_documents({"sentiment": "bullish"}) == len(world.news)


def test_gather_top_n_dedupes_headlines_and_numbers_sources(world):
    world.news = finnhub_news(4) + [{**finnhub_news(1)[0], "url": "https://news.example/dup"}]
    ticker, out = agent.gather_top_n_news("apple", n=10)
    assert ticker == "AAPL"
    assert [a["id"] for a in out] == ["S1", "S2", "S3", "S4"]
    assert all(a["textSource"] == "article" and a["origin"] == "news" for a in out)


def test_gather_top_n_tops_up_for_paywalls(world):
    world.pages = {"https://news.example/AAPL/0": "<p>Subscribe to read</p>",
                   "https://news.example/AAPL/1": "<html><article><p>" + "Teaser text here. " * 30
                                                  + "Subscribe to continue reading.</p></article></html>"}
    _, out = agent.gather_top_n_news("AAPL", n=2)
    assert [a["textSource"] for a in out] == ["summary", "teaser", "article", "article"]


def test_gather_top_n_respects_max_extra(world):
    world.pages = {a["url"]: None for a in world.news}   # every download fails
    _, out = agent.gather_top_n_news("AAPL", n=2, max_extra=1)
    assert len(out) == 3 and all(a["textSource"] == "summary" for a in out)


def test_article_text_downloaded_once(world):
    agent.gather_top_n_news("AAPL", n=3)
    pages = world.counts["page"]
    agent.gather_top_n_news("AAPL", n=3)
    assert world.counts["page"] == pages


def test_unknown_company_raises():
    import pytest
    with pytest.raises(ValueError):
        agent.gather_top_n_news("not a company")


# ---------- Web search ----------

def test_search_results_stored_and_cached(world):
    found = agent.gather_search_results("AAPL", ["Apple iPhone sales decline"])
    assert len(found) == 1 and found[0]["origin"] == "search" and found[0]["query"] == "Apple iPhone sales decline"
    assert found[0]["textSource"] == "article"
    agent.gather_search_results("AAPL", ["Apple iPhone sales decline"])
    assert world.counts["search"] == 1
    db.searches.update_one({}, {"$set": {"fetchedAt": db.now() - timedelta(hours=agent.SEARCH_HOURS + 1)}})
    agent.gather_search_results("AAPL", ["Apple iPhone sales decline"])
    assert world.counts["search"] == 2                     # stale cache is refreshed


def test_shared_search_cache_never_stores_user_words(world):
    words = "AAPL I work at Apple and the launch is slipping"
    world.search_results[words] = [{"title": "iPhone launch on track", "url": "https://web.example/launch",
                                    "content": "c", "source": "web.example", "date": None, "datetime": None}]
    assert agent.gather_search_results("AAPL", [words])
    for collection in (db.searches, db.news):
        for doc in collection.find():
            assert words not in str(doc)
    assert agent.query_key(words) == agent.query_key("  aapl i WORK at apple and the launch is slipping ")


def test_migrate_drops_old_search_index():
    db.searches.drop_indexes()
    db.searches.create_index([("ticker", 1), ("query", 1)], unique=True)
    db.searches.insert_one({"ticker": "AAPL", "query": "old raw text", "urls": []})
    db.news.insert_one({"ticker": "AAPL", "url": "u", "headline": "h", "queries": ["old raw text"]})
    db.init_indexes()
    assert "ticker_1_query_1" not in db.searches.index_information()
    assert db.searches.count_documents({}) == 0 and "queries" not in db.news.find_one()
    agent.gather_search_results("AAPL", ["q1"])
    agent.gather_search_results("AAPL", ["q2"])           # used to hit the old unique index
    assert db.searches.count_documents({}) == 2


def test_query_cap(world):
    agent.get_news_sentiments("AAPL", n=1, queries=[f"q{i}" for i in range(6)])
    assert world.counts["search"] == agent.MAX_ASSUMPTIONS


def test_news_window_uses_utc_day(monkeypatch):
    import datetime as dt
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 11, 2, 1, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(agent, "datetime", Clock)
    assert agent.resolve_dates(None, None)[1] == "2026-11-02"


def test_search_results_not_in_finnhub_feed(world):
    agent.gather_search_results("AAPL", ["Apple iPhone sales decline"])
    _, out = agent.gather_top_n_news("AAPL", n=10)
    assert all(a["origin"] == "news" for a in out) and len(out) == len(world.news)


def test_search_does_not_overwrite_finnhub_article(world):
    agent.gather_top_n_news("AAPL", n=1)
    finnhub_doc = db.news.find_one({"url": "https://news.example/AAPL/0"})
    world.search_results["q"] = [{"title": "Different title", "url": "https://news.example/AAPL/0", "content": "c",
                                  "source": "web", "date": None, "datetime": None}]
    found = agent.gather_search_results("AAPL", ["q"], already=[])
    doc = db.news.find_one({"url": "https://news.example/AAPL/0"})
    assert doc["headline"] == finnhub_doc["headline"] and doc["date"] == finnhub_doc["date"]
    assert "origin" not in doc
    assert found[0]["headline"] == finnhub_doc["headline"]


def test_finnhub_claims_url_found_by_search_first(world):
    world.search_results["q"] = [{"title": "AAPL news 0", "url": "https://news.example/AAPL/0", "content": "c",
                                  "source": "web", "date": None, "datetime": None}]
    agent.gather_search_results("AAPL", ["q"])
    _, out = agent.gather_top_n_news("AAPL", n=10)
    assert "https://news.example/AAPL/0" in [a["url"] for a in out]


def test_search_skips_already_seen(world):
    _, articles = agent.gather_top_n_news("AAPL", n=2)
    world.search_results["q"] = [{"title": articles[0]["headline"], "url": "https://other/1", "content": "",
                                  "source": "other", "date": None, "datetime": None},
                                 {"title": "New", "url": articles[1]["url"], "content": "",
                                  "source": "other", "date": None, "datetime": None}]
    assert agent.gather_search_results("AAPL", ["q"], articles) == []


def test_search_failure_is_skipped(world):
    world.search_error = RuntimeError("tavily down")
    assert agent.gather_search_results("AAPL", ["q1", "q2"]) == []


def test_get_news_sentiments_appends_search_sources(world, llm):
    ticker, results, counts = agent.get_news_sentiments("AAPL", n=3, queries=["Apple iPhone sales decline"])
    assert [a["id"] for a in results] == ["S1", "S2", "S3", "S4"]
    assert results[-1]["origin"] == "search"
    assert counts["bearish"] == 4 and sum(counts.values()) == 4
    assert all("_id" not in a for a in results)


# ---------- Sentiment ----------

def test_sentiment_is_cached_for_everyone(llm):
    agent.get_news_sentiments("AAPL", n=3)
    assert llm.roles().count("sentiment") == 3
    agent.get_news_sentiments("AAPL", n=3)
    assert llm.roles().count("sentiment") == 3
    assert db.news.count_documents({"sentiment": "bearish"}) == 3


def test_sentiment_synonyms(llm):
    llm.responses["sentiment"] = {"sentiment": "Positive!", "reason": "r"}
    _, results, counts = agent.get_news_sentiments("AAPL", n=1)
    assert results[0]["sentiment"] == "bullish" and counts["bullish"] == 1


def test_invalid_sentiment_not_cached(llm):
    llm.responses["sentiment"] = {"sentiment": "to the moon", "reason": "r"}
    _, results, _ = agent.get_news_sentiments("AAPL", n=1)
    assert results[0]["sentiment"] == "neutral"
    assert db.news.count_documents({"sentiment": None}) == db.news.count_documents({})
