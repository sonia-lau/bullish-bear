'''
MongoDB connection and collections.

Collections
- users       : { userId, generalRisk, mutedCategories, memoryOn, createdAt, updatedAt }
- news        : one doc per article per ticker, shared cache across users
                { ticker, url, headline, summary, source, date, datetime,
                  fullText, textSource: "article"|"teaser"|"summary",
                  sentiment, reason, sentimentModel, fetchedAt,
                  origin: "search" for web-search results (Finnhub news has no origin) }
- news_fetches: when each (ticker, start, end) window was last pulled from Finnhub
- searches    : when each (ticker, query) web search last ran { ticker, queryKey (sha256, not the
                  user's words), urls, fetchedAt }
- financials  : fact table per ticker { ticker, facts[{id, label, value, unit, period}], fetchedAt }
- companies   : peers per ticker, for "similar stocks you looked at" { ticker, peers, fetchedAt }
- sessions    : one analysis run { userId, ticker, reason, stockRisk, generalRisk,
                  sources, facts, assumptions, counts, summary, critique, remembered, createdAt }
- messages    : every LLM call { sessionId, userId, role, prompt, content, createdAt }
- theses      : why a user looked at a stock { userId, sessionId, stock, ticker, reason,
                  intent: "curious"|"signal", stockRisk, assumptions, peers, decision,
                  remembered, createdAt }
- objections  : devil's advocate points { sessionId, userId, ticker, category,
                  claim, sources, response, rebuttal, exchanges, remembered, createdAt }
'''

from datetime import datetime, timezone

from pymongo import MongoClient, ASCENDING, DESCENDING

import CONSTANTS

client = MongoClient(CONSTANTS.MONGODB_URI or "mongodb://127.0.0.1:27017")
db = client[CONSTANTS.MONGODB_DB or "bullish_bear"]

users = db["users"]
news = db["news"]
news_fetches = db["news_fetches"]
sessions = db["sessions"]
messages = db["messages"]
objections = db["objections"]
theses = db["theses"]
searches = db["searches"]
financials = db["financials"]
companies = db["companies"]


def now():
    return datetime.now(timezone.utc)


def migrate():
    '''One-off fixes for databases created by older versions.'''
    # Web searches used to be keyed by the raw query, which can hold a user's own words
    if "ticker_1_query_1" in searches.index_information():
        searches.drop_index("ticker_1_query_1")
    searches.delete_many({"queryKey": {"$exists": False}})
    news.update_many({"queries": {"$exists": True}}, {"$unset": {"queries": ""}})


def init_indexes():
    migrate()
    news.create_index([("ticker", ASCENDING), ("url", ASCENDING)], unique=True)
    news.create_index([("ticker", ASCENDING), ("datetime", DESCENDING)])
    news_fetches.create_index([("ticker", ASCENDING), ("start", ASCENDING), ("end", ASCENDING)], unique=True)
    users.create_index("userId", unique=True)
    sessions.create_index([("userId", ASCENDING), ("createdAt", DESCENDING)])
    messages.create_index([("sessionId", ASCENDING), ("createdAt", ASCENDING)])
    objections.create_index([("userId", ASCENDING), ("category", ASCENDING)])
    theses.create_index([("userId", ASCENDING), ("ticker", ASCENDING), ("createdAt", DESCENDING)])
    theses.create_index("sessionId")
    theses.create_index([("userId", ASCENDING), ("peers", ASCENDING)])
    searches.create_index([("ticker", ASCENDING), ("queryKey", ASCENDING)], unique=True)
    financials.create_index("ticker", unique=True)
    companies.create_index("ticker", unique=True)


def ping():
    return client.admin.command("ping")


init_indexes()
