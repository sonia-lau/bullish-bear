'''
Web news search (Tavily) for what the user claims, e.g. "negative press on iPhone sales".

Tavily is a search engine, not an LLM: the query is plain search words, and sentiment is
labelled afterwards by the sentiment agent, like any other source.
'''

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from tavily import TavilyClient

from CONSTANTS import TAVILY_API_KEY

tavily_client = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None


def parse_date(value):
    '''Tavily news dates look like "Sun, 01 Nov 2026 12:00:00 GMT"; sometimes ISO. None if missing.'''
    if not value:
        return None
    for parse in (parsedate_to_datetime, lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))):
        try:
            dt = parse(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            continue
    return None


def domain(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def search_news(query, max_results=3, time_range="month"):
    '''Recent news for a plain-words query. Returns [{title, url, content, source, date, datetime}].'''
    if tavily_client is None:
        raise RuntimeError("TAVILY_API_KEY is missing from .env")
    response = tavily_client.search(query, topic="news", time_range=time_range,
                                    max_results=max_results, search_depth="basic")
    results = []
    for r in response.get("results") or []:
        if not r.get("url") or not r.get("title"):
            continue
        dt = parse_date(r.get("published_date"))
        results.append({"title": r["title"], "url": r["url"], "content": r.get("content") or "",
                        "source": domain(r["url"]),
                        "date": dt.date().isoformat() if dt else None,
                        "datetime": int(dt.timestamp()) if dt else None})
    return results
