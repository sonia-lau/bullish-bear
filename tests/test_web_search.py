'''Tavily wrapper: response parsing (Tavily itself is faked).'''

import pytest

import web_search


class FakeTavily:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return self.response


@pytest.mark.parametrize("value, iso", [("Sun, 01 Nov 2026 12:00:00 GMT", "2026-11-01"),
                                        ("2026-10-30T08:00:00Z", "2026-10-30"),
                                        ("2026-10-30", "2026-10-30")])
def test_parse_date(value, iso):
    assert web_search.parse_date(value).date().isoformat() == iso


@pytest.mark.parametrize("value", [None, "", "yesterday"])
def test_parse_date_bad(value):
    assert web_search.parse_date(value) is None


def test_search_news_parses_results(monkeypatch):
    fake = FakeTavily({"results": [
        {"title": "iPhone sales slump", "url": "https://www.reuters.com/a", "content": "Sales fell.",
         "published_date": "Sun, 01 Nov 2026 12:00:00 GMT"},
        {"title": "No date", "url": "https://bloomberg.com/b"},
        {"title": "", "url": "https://x.com/c"},          # dropped: no title
        {"title": "No url"},                              # dropped: no url
    ]})
    monkeypatch.setattr(web_search, "tavily_client", fake)
    out = web_search.search_news("Apple iPhone sales", max_results=2)
    assert [r["title"] for r in out] == ["iPhone sales slump", "No date"]
    assert out[0]["source"] == "reuters.com" and out[0]["date"] == "2026-11-01" and out[0]["datetime"]
    assert out[1]["date"] is None and out[1]["content"] == ""
    query, kwargs = fake.calls[0]
    assert query == "Apple iPhone sales" and kwargs["topic"] == "news" and kwargs["max_results"] == 2


def test_search_news_empty_response(monkeypatch):
    monkeypatch.setattr(web_search, "tavily_client", FakeTavily({}))
    assert web_search.search_news("q") == []


def test_search_news_without_key(monkeypatch):
    monkeypatch.setattr(web_search, "tavily_client", None)
    with pytest.raises(RuntimeError):
        web_search.search_news("q")
