'''
MongoDB connection and collections.

Collections
- users       : { userId, generalRisk, mutedCategories, createdAt, updatedAt }
- news        : one doc per article per ticker, shared cache across users
                { ticker, url, headline, summary, source, date, datetime,
                  fullText, textSource: "article"|"teaser"|"summary",
                  sentiment, reason, sentimentModel, fetchedAt }
- news_fetches: when each (ticker, start, end) window was last pulled from Finnhub
- sessions    : one analysis run { userId, ticker, reason, stockRisk, generalRisk,
                  sources, counts, summary, critique, createdAt }
- messages    : every LLM call { sessionId, userId, role, prompt, content, createdAt }
- theses      : why a user looked at a stock { userId, sessionId, stock, ticker, reason,
                  intent: "curious"|"signal", stockRisk, decision, createdAt }
- objections  : devil's advocate points { sessionId, userId, ticker, category,
                  claim, sources, response, rebuttal, exchanges, createdAt }
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


def now():
    return datetime.now(timezone.utc)


def init_indexes():
    news.create_index([("ticker", ASCENDING), ("url", ASCENDING)], unique=True)
    news.create_index([("ticker", ASCENDING), ("datetime", DESCENDING)])
    news_fetches.create_index([("ticker", ASCENDING), ("start", ASCENDING), ("end", ASCENDING)], unique=True)
    users.create_index("userId", unique=True)
    sessions.create_index([("userId", ASCENDING), ("createdAt", DESCENDING)])
    messages.create_index([("sessionId", ASCENDING), ("createdAt", ASCENDING)])
    objections.create_index([("userId", ASCENDING), ("category", ASCENDING)])
    theses.create_index([("userId", ASCENDING), ("ticker", ASCENDING), ("createdAt", DESCENDING)])
    theses.create_index("sessionId")


def ping():
    return client.admin.command("ping")


init_indexes()
