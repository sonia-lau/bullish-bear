'''
Shared test setup. Every test gets:
- an empty throwaway Mongo database (bullish_bear_test_<pid>, dropped afterwards), never the real one
- fake Finnhub, Tavily, article downloads and LLM, so nothing touches the network

Run from the project root:  .venv/bin/python -m pytest
Needs the local MongoDB running.
'''

import json
import os
import sys
import time
import types

# Must be set before CONSTANTS loads .env (load_dotenv doesn't override existing variables).
# One database per run, so two test runs at once don't wipe each other's data
os.environ["MONGODB_DB"] = f"bullish_bear_test_{os.getpid()}"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "utils"))

import pytest

import CONSTANTS
import db

assert db.db.name.startswith("bullish_bear_test_"), "tests must never run against the real database"

import agent
import financials

REAL_CALL_AGENT = agent.call_agent
NOW = int(time.time())
ARTICLE_HTML = "<html><body><article><p>" + "Apple reported strong iPhone sales this quarter. " * 40 + "</p></article></body></html>"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    db.client.drop_database(db.db.name)
    db.init_indexes()
    yield
    db.client.drop_database(db.db.name)


@pytest.fixture(autouse=True)
def clean_db(test_database):
    # Emptying is much faster than dropping, and keeps the unique indexes the code relies on
    for name in db.db.list_collection_names():
        db.db[name].delete_many({})


class FakeLLM:
    '''Answers by role (sentiment, researcher, critic, devil, rebuttal, assumptions).
    A response can be a string, a dict (sent as JSON) or a function of the prompt.'''

    DEFAULTS = {
        "sentiment": {"sentiment": "bearish", "reason": "Sales are slowing."},
        "assumptions": {"assumptions": [{"text": "iPhone sales are falling", "query": "Apple iPhone sales decline"}]},
        "researcher": {"summary": "Mixed news.", "overall_sentiment": "bearish",
                       "key_points": [{"point": "P/E is 30.0", "sources": ["S1", "F3"]}]},
        "critic": {"assumptions": [{"id": "A1", "status": "supported", "note": "S1 reports it.", "sources": ["S1"]}],
                   "issues": [{"target": "user", "issue": "Only one source.", "sources": ["S1"]}],
                   "verdict": "Partly supported."},
        "devil": {"objections": [{"category": "valuation", "claim": "P/E of 30.0 is rich.", "sources": ["S1", "F3"]},
                                 {"category": "debt", "claim": "Debt is rising.", "sources": ["S2"]}]},
        "rebuttal": {"verdict": "hold", "reply": "Not convinced.", "sources": ["S1"]},
    }

    def __init__(self):
        self.responses = dict(self.DEFAULTS)
        self.calls = []

    def __call__(self, prompt, system=None, session_id=None, user_id=None, role="assistant"):
        self.calls.append({"role": role, "prompt": prompt, "system": system})
        reply = self.responses.get(role, {})
        if callable(reply):
            reply = reply(prompt)
        content = reply if isinstance(reply, str) else json.dumps(reply)
        if session_id is not None:
            db.messages.insert_one({"sessionId": session_id, "userId": user_id, "role": role,
                                    "prompt": prompt, "content": content, "createdAt": db.now()})
        return content

    def roles(self):
        return [c["role"] for c in self.calls]

    def prompts(self, role):
        return [c["prompt"] for c in self.calls if c["role"] == role]


def finnhub_news(n=6, ticker="AAPL"):
    return [{"id": i, "headline": f"{ticker} news {i}", "summary": f"summary {i}", "source": "Yahoo",
             "url": f"https://news.example/{ticker}/{i}", "datetime": NOW - i * 3600} for i in range(n)]


METRICS = {"peTTM": 30.0, "revenueGrowthTTMYoy": -1.2, "netProfitMarginTTM": 24.3,
           "totalDebt/totalEquityQuarterly": 1.5, "52WeekHigh": 260.1, "52WeekLow": 164.08,
           "marketCapitalization": 3500000.0}
QUOTE = {"c": 230.5, "dp": -0.8}


class FakeWorld:
    '''Fake external services. Change attributes in a test to change what they return.'''

    def __init__(self):
        self.news = finnhub_news()
        self.metrics = dict(METRICS)
        self.quote = dict(QUOTE)
        self.peers = ["AAPL", "MSFT", "GOOGL"]
        self.companies = {"apple": "AAPL", "aapl": "AAPL", "msft": "MSFT", "microsoft": "MSFT",
                          "googl": "GOOGL", "tsla": "TSLA", "nvda": "NVDA"}
        self.search_results = {}   # query -> list of results; default makes one result per query
        self.search_error = None
        self.pages = {}            # url -> html; default is a full article
        self.counts = {"news": 0, "metrics": 0, "peers": 0, "search": 0, "page": 0}

    def find_company(self, stock):
        key = str(stock).strip().lower()
        if key not in self.companies:
            raise ValueError(f"No company found for '{stock}'")
        return self.companies[key]

    def get_company_news(self, ticker, start=None, end=None):
        self.counts["news"] += 1
        return self.news

    def get_basic_financials(self, ticker, metric="all"):
        self.counts["metrics"] += 1
        return {"metric": self.metrics}

    def get_quote(self, ticker):
        return self.quote

    def get_peers(self, ticker):
        self.counts["peers"] += 1
        return self.peers

    def search_news(self, query, max_results=3, time_range="month"):
        self.counts["search"] += 1
        if self.search_error:
            raise self.search_error
        if query in self.search_results:
            return self.search_results[query]
        slug = "-".join(query.lower().split())[:60]
        return [{"title": f"Web: {query}", "url": f"https://web.example/{slug}", "content": f"About {query}.",
                 "source": "web.example", "date": "2026-10-30", "datetime": NOW - 86400}]

    def get(self, url, **kwargs):
        self.counts["page"] += 1
        html = self.pages.get(url, ARTICLE_HTML)
        return types.SimpleNamespace(ok=html is not None, text=html or "")


@pytest.fixture(autouse=True)
def world(monkeypatch):
    w = FakeWorld()
    monkeypatch.setattr(agent, "find_company", w.find_company)
    monkeypatch.setattr(agent, "get_company_news", w.get_company_news)
    monkeypatch.setattr(agent, "search_news", w.search_news)
    monkeypatch.setattr(agent.requests, "get", w.get)
    monkeypatch.setattr(financials, "get_company_basic_financials", w.get_basic_financials)
    monkeypatch.setattr(financials, "get_quote", w.get_quote)
    monkeypatch.setattr(financials, "get_company_peers", w.get_peers)
    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: pytest.fail("real LLM call attempted"))
    return w


@pytest.fixture(autouse=True)
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(agent, "call_agent", fake)
    return fake
