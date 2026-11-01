'''
Agents
- extract_assumptions : turn the user's reason into checkable claims, each with a search query
- get_news_sentiments : Finnhub news + web search on the user's claims, each labelled
                        bullish / bearish / neutral / irrelevant
- researcher_summary  : sourced summary citing [S#] sources and [F#] financial facts
- critic              : checks each of the user's assumptions, pokes holes in the summary
- devils_advocate     : argues against buying, user's blind-spot categories first
- rebuttal_reply      : devil's advocate answers when the user argues back (searches their claim)
- thesis_checkin      : revisits a past thesis: news since then vs each assumption (/checkin)
- run_analysis        : runs all of the above for one user and saves to MongoDB
'''

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from itertools import islice

import requests
import trafilatura
from bson import ObjectId
from pymongo import UpdateOne

import CONSTANTS
import db
import financials
import user
from financial_news import find_company, get_company_news
from web_search import search_news

SENTIMENTS = ["bullish", "bearish", "neutral", "irrelevant"]
CATEGORIES = ["valuation", "growth", "margins", "debt", "competition",
              "management", "macro", "sentiment", "other"]
CACHE_HOURS = 6
NEWS_DAYS = 10
SEARCH_HOURS = 6
SEARCH_RESULTS = 3   # per query
MAX_ASSUMPTIONS = 3
ASSUMPTION_STATUSES = ["supported", "contradicted", "mixed", "no_evidence"]

log = logging.getLogger("bullish-bear.agent")


# ---------- LLM ----------

def call_agent(prompt, system=None, session_id=None, user_id=None, role="assistant"):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    # Retry rate limits and server errors with backoff
    for attempt in range(3):
        response = requests.post(url="https://openrouter.ai/api/v1/chat/completions",
                                 headers={"Authorization": "Bearer " + CONSTANTS.OPENROUTER_API_KEY,
                                          "Content-Type": "application/json"},
                                 data=json.dumps({"model": CONSTANTS.MODEL, "messages": msgs}),
                                 timeout=120)
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(int(response.headers.get("Retry-After", 2 ** (attempt + 1))))
            continue
        break
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"OpenRouter {response.status_code}: {response.text[:300]}")
    if "error" in data:
        raise RuntimeError(f"OpenRouter error: {data['error']}")
    if not data.get("choices"):
        raise RuntimeError(f"OpenRouter returned no choices: {str(data)[:300]}")
    content = data["choices"][0]["message"].get("content") or ""

    # Keep the conversation so it can be referenced later
    if session_id is not None:
        db.messages.insert_one({"sessionId": session_id, "userId": user_id, "role": role,
                                "system": system, "prompt": prompt, "content": content,
                                "createdAt": db.now()})
    return content


def parse_json(text):
    '''Pull the first JSON object out of an LLM reply (handles ```json fences and extra text).'''
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in reply: {text[:200]}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def call_agent_json(prompt, system=None, retries=1, **log):
    last_error = None
    for _ in range(retries + 1):
        reply = call_agent(prompt, system, **log)
        try:
            result = parse_json(reply)
            if not isinstance(result, dict):
                raise ValueError("Reply was JSON but not an object")
            return result
        except ValueError as e:
            last_error = e
            prompt += "\n\nYour last reply was not valid JSON. Reply with only the JSON object."
    raise last_error


# ---------- News (cached in MongoDB) ----------

def resolve_dates(start, end):
    # Article dates are stored as UTC dates, so "today" must be the UTC day too
    end = end or str(datetime.now(timezone.utc).date())
    start = start or str(date.fromisoformat(end) - timedelta(days=NEWS_DAYS))
    return start, end


def fetch_news_to_cache(ticker, start, end):
    '''Pull news from Finnhub unless this window was fetched in the last CACHE_HOURS.'''
    last = db.news_fetches.find_one({"ticker": ticker, "start": start, "end": end})
    if last:
        fetched = last["fetchedAt"]
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if db.now() - fetched < timedelta(hours=CACHE_HOURS):
            return False

    ops = []
    for a in get_company_news(ticker, start, end):
        if not a.get("url") or not a.get("headline"):
            continue
        ops.append(UpdateOne(
            {"ticker": ticker, "url": a["url"]},
            # $set refreshes article text, $setOnInsert keeps any sentiment already computed
            {"$set": {"headline": a["headline"],
                      "summary": a.get("summary") or "",
                      "source": a.get("source"),
                      "datetime": a["datetime"],
                      "date": datetime.fromtimestamp(a["datetime"], timezone.utc).date().isoformat(),
                      "finnhubId": a.get("id"),
                      "fetchedAt": db.now()},
             "$setOnInsert": {"sentiment": None, "reason": None},
             # Finnhub owns any url a web search found first, so it shows up in the news feed
             "$unset": {"origin": ""}},
            upsert=True))
    if ops:
        db.news.bulk_write(ops, ordered=False)
    db.news_fetches.update_one({"ticker": ticker, "start": start, "end": end},
                               {"$set": {"fetchedAt": db.now()}}, upsert=True)
    return True


def article_from_doc(doc, origin, query=None):
    return {"_id": doc["_id"],
            "headline": doc["headline"],
            "summary": doc.get("summary") or "",
            "source": doc.get("source") or "unknown",
            "url": doc["url"],
            "date": doc.get("date") or "undated",
            "origin": origin,
            "query": query,
            "sentiment": doc.get("sentiment"),
            "reason": doc.get("reason")}


def gather_top_n_news(stock, n=10, start=None, end=None, max_extra=None, ticker=None):
    '''Newest articles until n of them have full text. Teasers and summary-only articles are
    kept for context but don't count towards n. At most n + max_extra articles in total.'''
    ticker = ticker or find_company(stock)
    start, end = resolve_dates(start, end)
    fetch_news_to_cache(ticker, start, end)
    max_extra = n if max_extra is None else max_extra

    def candidates():
        seen = set()
        # Web-search results live in the same collection but aren't part of the news feed
        query = {"ticker": ticker, "date": {"$gte": start, "$lte": end}, "origin": {"$ne": "search"}}
        for a in db.news.find(query).sort("datetime", -1):
            if a["headline"] in seen:
                continue
            seen.add(a["headline"])
            yield article_from_doc(a, origin="news")

    pool, output, full = candidates(), [], 0
    while full < n and len(output) < n + max_extra:
        # Only download as many as are still needed, so paywalls cost one extra fetch each
        batch = list(islice(pool, min(n - full, n + max_extra - len(output))))
        if not batch:
            break
        add_article_text(batch)
        for a in batch:
            a["id"] = f"S{len(output) + 1}"
            output.append(a)
            full += a["textSource"] == "article"
    return ticker, output


# ---------- Web search on the user's claims (cached in MongoDB) ----------

def query_key(query):
    '''Queries can hold the user's own words (a rebuttal), so the shared cache keeps only a hash.'''
    return hashlib.sha256(" ".join(query.lower().split()).encode()).hexdigest()


def search_to_cache(ticker, query):
    '''Run a web news search unless the same query ran in the last SEARCH_HOURS. Returns result urls.'''
    key = query_key(query)
    last = db.searches.find_one({"ticker": ticker, "queryKey": key})
    if last:
        fetched = last["fetchedAt"]
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if db.now() - fetched < timedelta(hours=SEARCH_HOURS):
            return last["urls"]

    results = search_news(query, max_results=SEARCH_RESULTS)
    ops = [UpdateOne({"ticker": ticker, "url": r["url"]},
                     # Only new urls take Tavily's fields: a url Finnhub already gave us keeps
                     # its Finnhub date and source, and stays a Finnhub article
                     {"$setOnInsert": {"headline": r["title"], "summary": r["content"], "source": r["source"],
                                       "date": r["date"], "datetime": r["datetime"], "fetchedAt": db.now(),
                                       "origin": "search", "sentiment": None, "reason": None}},
                     upsert=True)
           for r in results]
    if ops:
        db.news.bulk_write(ops, ordered=False)
    urls = [r["url"] for r in results]
    db.searches.update_one({"ticker": ticker, "queryKey": key},
                           {"$set": {"urls": urls, "fetchedAt": db.now()}}, upsert=True)
    return urls


def gather_search_results(ticker, queries, already=()):
    '''Web results for each query, skipping anything already in `already`. A failed search is
    skipped, not fatal: the run still has the Finnhub news.'''
    seen_urls = {a["url"] for a in already}
    seen_headlines = {a["headline"] for a in already}
    output = []
    for query in queries:
        try:
            urls = search_to_cache(ticker, query)
        except Exception as e:
            log.warning("Web search failed for %r: %s", query, e)
            continue
        docs = {d["url"]: d for d in db.news.find({"ticker": ticker, "url": {"$in": urls}})}
        for url in urls:
            doc = docs.get(url)
            if not doc or url in seen_urls or doc["headline"] in seen_headlines:
                continue
            seen_urls.add(url)
            seen_headlines.add(doc["headline"])
            output.append(article_from_doc(doc, origin="search", query=query))
    return add_article_text(output) if output else output


def next_source_id(results):
    return f'S{max([int(a["id"][1:]) for a in results if str(a.get("id", "")).startswith("S")] or [0]) + 1}'


# ---------- Full article text (cached on the news doc) ----------

ARTICLE_CHARS = 10000


TEASER_MARKERS = ["continue reading", "subscribe", "sign in to read", "log in to read",
                  "premium", "for subscribers", "unlock this article", "read the full"]


def classify_text(text):
    '''"article" for a full read, "teaser" for a paywall preview, None if nothing usable.'''
    # Very short extractions are usually cookie walls or "subscribe to read" pages
    if not text or len(text) < 300:
        return None
    if len(text) < 1500 and any(m in text.lower() for m in TEASER_MARKERS):
        return "teaser"
    return "article"


def fetch_article_text(url, max_chars=ARTICLE_CHARS):
    '''Download the page and extract the article. Returns (text, "article" | "teaser" | "summary").'''
    try:
        r = requests.get(url, timeout=10, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        text = trafilatura.extract(r.text) if r.ok else None
    except Exception:
        text = None
    kind = classify_text(text)
    return (text[:max_chars], kind) if kind else (None, "summary")


def add_article_text(articles):
    '''Fill article["text"] with the full article (or paywall teaser), falling back to the summary.
    Each url is downloaded once and cached for everyone.'''
    stored = {d["_id"]: d for d in db.news.find({"_id": {"$in": [a["_id"] for a in articles]}},
                                                 {"fullText": 1, "textSource": 1})}
    missing = [a for a in articles if not stored.get(a["_id"], {}).get("textSource")]

    with ThreadPoolExecutor(max_workers=5) as pool:
        texts = dict(zip([a["_id"] for a in missing], pool.map(lambda a: fetch_article_text(a["url"]), missing)))
    for _id, (text, kind) in texts.items():
        stored[_id] = {"fullText": text, "textSource": kind}
        db.news.update_one({"_id": _id}, {"$set": stored[_id]})

    # Re-check texts cached before teaser detection existed
    for _id, doc in stored.items():
        if doc.get("textSource") == "article" and classify_text(doc.get("fullText")) == "teaser":
            doc["textSource"] = "teaser"
            db.news.update_one({"_id": _id}, {"$set": {"textSource": "teaser"}})

    for a in articles:
        doc = stored.get(a["_id"], {})
        a["textSource"] = doc.get("textSource", "summary")
        a["text"] = doc.get("fullText") or a["summary"] or a["headline"]
    return articles


# ---------- Sentiment ----------

TEXT_LABELS = {"article": "Article text", "teaser": "Article preview (paywalled, partial)",
               "summary": "Summary"}

SENTIMENT_SYSTEM = ("You are a financial news analyst. You judge how a single news article "
                    "affects one specific stock. Reply with JSON only.")


def article_sentiment(ticker, article, **log):
    prompt = f'''Stock: {ticker}
Article date: {article["date"]}
Source: {article["source"]}
Headline: {article["headline"]}
{TEXT_LABELS.get(article.get("textSource"), "Summary")}: {article.get("text") or article.get("summary") or "(no text)"}

Label the article's sentiment for {ticker}:
- bullish: good news for {ticker}'s share price
- bearish: bad news for {ticker}'s share price
- neutral: about {ticker} but no clear direction
- irrelevant: {ticker} is only mentioned in passing or not at all

Reply with JSON only: {{"sentiment": "bullish|bearish|neutral|irrelevant", "reason": "one sentence"}}'''
    result = call_agent_json(prompt, SENTIMENT_SYSTEM, **log)
    sentiment = re.sub(r"[^a-z]", "", str(result.get("sentiment") or "").lower())
    sentiment = SENTIMENT_SYNONYMS.get(sentiment, sentiment)
    reason = str(result.get("reason") or "").strip()
    # None means the label was unusable: don't cache it, so the next run asks again
    return (sentiment if sentiment in SENTIMENTS else None), reason


SENTIMENT_SYNONYMS = {"positive": "bullish", "negative": "bearish", "mixed": "neutral"}


def label_sentiments(ticker, articles, session_id=None, user_id=None):
    '''Label each article, cached on the news doc and reused by everyone. Returns (results, counts).'''
    results = []
    counts = {s: 0 for s in SENTIMENTS}
    for article in articles:
        if article["sentiment"] not in SENTIMENTS:
            # Another user may have labelled it while we were busy
            stored = db.news.find_one({"_id": article["_id"]}, {"sentiment": 1, "reason": 1}) or {}
            if stored.get("sentiment") in SENTIMENTS:
                article["sentiment"], article["reason"] = stored["sentiment"], stored.get("reason")
        if article["sentiment"] not in SENTIMENTS:
            sentiment, reason = article_sentiment(ticker, article, session_id=session_id,
                                                  user_id=user_id, role="sentiment")
            if sentiment is not None:
                db.news.update_one({"_id": article["_id"]},
                                   {"$set": {"sentiment": sentiment, "reason": reason,
                                             "sentimentModel": CONSTANTS.MODEL}})
            article["sentiment"], article["reason"] = sentiment or "neutral", reason
        counts[article["sentiment"]] += 1
        results.append({k: v for k, v in article.items() if k != "_id"})
    return results, counts


def get_news_sentiments(stock, n=10, start=None, end=None, queries=None, ticker=None,
                        session_id=None, user_id=None):
    '''Finnhub news plus web results for each query (the user's claims). Returns (ticker, results, counts).'''
    ticker, articles = gather_top_n_news(stock, n, start, end, ticker=ticker)
    for a in gather_search_results(ticker, (queries or [])[:MAX_ASSUMPTIONS], articles):
        a["id"] = next_source_id(articles)
        articles.append(a)
    results, counts = label_sentiments(ticker, articles, session_id, user_id)
    return ticker, results, counts


# ---------- Helpers shared by the agents ----------

def visible(results):
    '''Sources the agents actually see; irrelevant articles are left out.'''
    return [a for a in results if a["sentiment"] != "irrelevant"]


def format_sources(results):
    def found_by(a):
        return f' | web search: "{a["query"]}"' if a.get("origin") == "search" and a.get("query") else ""
    return "\n".join(f'{a["id"]}: {a.get("date") or "undated"} | {a["source"]} | {a["sentiment"]}{found_by(a)} | '
                     f'{a["headline"]} — {(a.get("text") or a.get("summary") or "")[:800]}'
                     for a in visible(results)) or "(no news sources)"


def format_facts(facts):
    return financials.format_facts(facts) if facts else "(no financial data)"


def format_assumptions(assumptions, with_status=False):
    def status(a):
        return f' ({a["status"].replace("_", " ")})' if with_status and a.get("status") else ""
    return "\n".join(f'{a["id"]}: {a["text"]}{status(a)}' for a in assumptions or []) or "(none given)"


def as_list(value):
    '''LLMs sometimes return null, a single object or a string where a list was asked for.'''
    if isinstance(value, list):
        return value
    if isinstance(value, (dict, str, int)):
        return [value]
    return []


CITATION = re.compile(r"([SF])\s*(\d+)", re.I)


def item_text(item):
    return " ".join(str(item.get(k) or "") for k in ("point", "claim", "issue", "reply", "note"))


def check_citations(items, results, key="sources", facts=None):
    '''Code check: keep only ids the agent was shown (S# sources, F# facts), and flag items left
    with none. Numbers next to an F# citation are checked against the fact table:
    numberCheck is "verified", "mismatch" or None (nothing to check).'''
    valid_ids = {a["id"] for a in visible(results)} | {f["id"] for f in facts or []}
    checked = []
    for item in as_list(items):
        if isinstance(item, str):
            item = {"point": item, key: []}
        if not isinstance(item, dict):
            continue
        # Accept "S1", "[S1]", "s1", "S1, F2" or a bare 1
        found = []
        for x in as_list(item.get(key)):
            ids = [f"S{x}"] if isinstance(x, int) else [f"{k.upper()}{n}" for k, n in CITATION.findall(str(x))]
            found += [i for i in ids if i in valid_ids]
        item[key] = list(dict.fromkeys(found))
        item["unverified"] = not item[key]
        fact_ids = [i for i in item[key] if i.startswith("F")]
        item["numberCheck"] = financials.check_numbers(item_text(item), fact_ids, facts) if fact_ids else None
        checked.append(item)
    return checked


INTENT_TEXT = {"curious": "just curious about the stock",
               "signal": "looking for a reason to buy or sell"}


def user_context(reason, stock_risk, general_risk, intent=None, memory=None):
    text = (f"User's reason for looking at this stock: {reason or 'not given'}\n"
            f"User is {INTENT_TEXT.get(intent, 'not sure why they are looking')}\n"
            f"User's risk appetite for this stock: {stock_risk or 'not given'}\n"
            f"User's general risk appetite: {general_risk or 'not given'}")
    if memory:
        text += f"\n\nWhat we remember about this user:\n{memory}"
    return text


# ---------- Assumptions (what the user's reason depends on) ----------

ASSUMPTION_SYSTEM = ("You help an investor make their reasoning checkable. Turn their reason for "
                     "looking at a stock into the specific factual claims it depends on, each one "
                     "something news or financial data could confirm or contradict. Keep the user's "
                     "meaning; don't add claims they didn't make. Reply with JSON only.")


def make_assumptions(ticker, items):
    '''Normalise assumptions (from the LLM or typed by the user) to [{id, text, query}].'''
    out = []
    for item in as_list(items):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or item.get("assumption") or "").split())
        if not text:
            continue
        query = " ".join(str(item.get("query") or "").split()) or f"{ticker} {text}"
        out.append({"id": f"A{len(out) + 1}", "text": text[:300], "query": query[:150]})
        if len(out) == MAX_ASSUMPTIONS:
            break
    return out


def extract_assumptions(ticker, reason, intent=None, user_id=None):
    if not reason or not reason.strip():
        return []
    prompt = f'''Stock: {ticker}
The user is {INTENT_TEXT.get(intent, "looking at this stock")}.
User's reason: {reason}

List 1-3 claims the reason depends on. For each, give a short web news search query (plain words,
include the company name) that would find evidence for or against it.
Reply with JSON only:
{{"assumptions": [{{"text": "iPhone sales are falling", "query": "Apple iPhone sales decline"}}]}}'''
    result = call_agent_json(prompt, ASSUMPTION_SYSTEM, user_id=user_id, role="assumptions")
    return make_assumptions(ticker, result.get("assumptions"))


# ---------- Researcher ----------

RESEARCHER_SYSTEM = ("You are an equity researcher. Summarise only what the sources and financial facts "
                     "say. Every key point must cite ids: S1 for news sources, F1 for financial facts. "
                     "When you use a number from a fact, copy it and cite its F id. Never invent facts, "
                     "numbers or sources. Do not give buy or sell advice. Reply with JSON only.")


def researcher_summary(ticker, results, counts, facts=None, **log):
    prompt = f'''Stock: {ticker}
Sentiment counts: {json.dumps(counts)}

Financial facts:
{format_facts(facts)}

Sources:
{format_sources(results)}

Write a short, balanced summary of the recent news and financials for {ticker}.
Reply with JSON only:
{{"summary": "3-5 sentences",
  "key_points": [{{"point": "one sentence", "sources": ["S1", "F2"]}}],
  "overall_sentiment": "bullish|bearish|neutral|mixed"}}'''
    result = call_agent_json(prompt, RESEARCHER_SYSTEM, **log)
    result["key_points"] = check_citations(result.get("key_points"), results, facts=facts)
    return result


# ---------- Critic ----------

CRITIC_SYSTEM = ("You are a strict reviewer. You did not write the summary and your job is to "
                 "find problems in reasoning, not to argue a side. Check each of the user's assumptions "
                 "against the sources and financial facts, whether claims are supported by what they "
                 "cite, and whether dates or periods are mixed up (e.g. a quarterly figure used as "
                 "annual). If the sources don't cover a claim, mark it no_evidence: that does not make "
                 "the user wrong, so never call a claim false only because the sources are silent. "
                 "Reply with JSON only.")

STATUS_SYNONYMS = {"support": "supported", "true": "supported", "confirmed": "supported",
                   "contradict": "contradicted", "false": "contradicted", "refuted": "contradicted",
                   "partial": "mixed", "partiallysupported": "mixed", "unclear": "no_evidence",
                   "noevidence": "no_evidence", "unknown": "no_evidence", "notcovered": "no_evidence"}


def check_assumptions(assumptions, reviewed, results, facts=None):
    '''Merge the critic's verdicts onto the user's assumptions; any it skipped become no_evidence.'''
    by_id = {}
    for r in as_list(reviewed):
        if isinstance(r, dict):
            ids = re.findall(r"A\s*(\d+)", str(r.get("id") or ""), re.I)
            if ids:
                by_id.setdefault(f"A{ids[0]}", r)
    merged = []
    for a in assumptions or []:
        r = by_id.get(a["id"], {})
        status = re.sub(r"[^a-z]", "", str(r.get("status") or "").lower())
        status = STATUS_SYNONYMS.get(status, status)
        note = str(r.get("note") or "").strip() or ("" if r else "The critic didn't assess this.")
        merged.append({**a, "status": status if status in ASSUMPTION_STATUSES else "no_evidence",
                       "note": note, "sources": r.get("sources")})
    return check_citations(merged, results, facts=facts)


def critic(ticker, results, summary, reason, stock_risk, general_risk, intent=None, memory=None,
           assumptions=None, facts=None, **log):
    prompt = f'''Stock: {ticker}
{user_context(reason, stock_risk, general_risk, intent, memory)}

User's assumptions to check:
{format_assumptions(assumptions)}

Researcher's summary:
{json.dumps(summary, indent=2)}

Financial facts:
{format_facts(facts)}

Sources:
{format_sources(results)}

For each assumption give a status (supported, contradicted, mixed or no_evidence) and a one-sentence note.
Then list other problems. target is "user" if it's about the user's reason, "researcher" if it's about the summary.
Reply with JSON only:
{{"assumptions": [{{"id": "A1", "status": "supported|contradicted|mixed|no_evidence", "note": "one sentence", "sources": ["S2", "F1"]}}],
  "issues": [{{"target": "user|researcher", "issue": "one sentence", "sources": ["S2"]}}],
  "verdict": "one sentence on how well-supported the user's reason is"}}'''
    result = call_agent_json(prompt, CRITIC_SYSTEM, **log)
    issues = [i for i in as_list(result.get("issues")) if isinstance(i, dict) and i.get("issue")]
    for i in issues:
        target = str(i.get("target") or "").strip().lower()
        i["target"] = target if target in ("user", "researcher") else "researcher"
    result["issues"] = check_citations(issues, results, facts=facts)
    result["assumptions"] = check_assumptions(assumptions, result.get("assumptions"), results, facts)
    return result


# ---------- Devil's advocate ----------

DEVIL_SYSTEM = ("You are a devil's advocate. The user is leaning towards this stock; argue the "
                "strongest case against it using only the sources and financial facts given. Weigh "
                "each risk against the user's risk appetite. Every objection must cite ids (S# sources, "
                "F# facts) and copy numbers from facts exactly. Argue against the user's position, but "
                "don't dismiss a claim of theirs just because the sources don't mention it. "
                "Reply with JSON only.")


def devils_advocate(ticker, results, summary, reason, stock_risk, general_risk, blind_spots,
                    intent=None, memory=None, assumptions=None, facts=None, **log):
    spots = [c for c, v in blind_spots.items() if v["dismissed"] > 0]
    spot_text = (f"The user has often dismissed these kinds of points before: {', '.join(spots)}. "
                 "Include an objection for these if the sources support one."
                 if spots else "No history of dismissed points yet.")
    prompt = f'''Stock: {ticker}
{user_context(reason, stock_risk, general_risk, intent, memory)}
{spot_text}

User's assumptions and how the evidence stands:
{format_assumptions(assumptions, with_status=True)}

Researcher's summary:
{json.dumps(summary, indent=2)}

Financial facts:
{format_facts(facts)}

Sources:
{format_sources(results)}

Give 3-5 objections against the user's reason. category must be one of: {", ".join(CATEGORIES)}.
Reply with JSON only:
{{"objections": [{{"category": "valuation", "claim": "one or two sentences", "sources": ["S3", "F1"]}}]}}'''
    result = call_agent_json(prompt, DEVIL_SYSTEM, **log)
    items = [o for o in as_list(result.get("objections")) if isinstance(o, dict) and o.get("claim")]
    for o in items:
        category = str(o.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            category = next((c for c in CATEGORIES if c != "other" and c in category), "other")
        o["category"] = category
    items = check_citations(items, results, facts=facts)

    # Blind-spot ranking is done in code: most-dismissed categories first, with the reason shown
    for o in items:
        spot = blind_spots.get(o["category"])
        o["dismissedBefore"] = spot["dismissed"] if spot else 0
        o["whyFirst"] = (f'You dismissed {spot["dismissed"]} of {spot["total"]} {o["category"]} points before'
                         if spot and spot["dismissed"] else None)
    items.sort(key=lambda o: o["dismissedBefore"], reverse=True)
    return items


# ---------- Rebuttal (user argues back) ----------

REBUTTAL_SYSTEM = ("You are the devil's advocate continuing a debate. The user has pushed back on one of "
                   "your objections. Judge their rebuttal honestly against the sources and financial facts, "
                   "including any new sources found by searching their claim: concede if it holds up, hold "
                   "your ground if it doesn't, and say why in two or three sentences. If nothing covers "
                   "their claim, say you couldn't confirm it rather than calling it wrong. Cite ids. "
                   "Reply with JSON only.")


def session_source(a):
    '''What a session keeps of each source, so rebuttals can be argued against the same evidence.'''
    return ({k: a.get(k) for k in ("id", "url", "headline", "source", "date", "sentiment",
                                   "textSource", "origin", "query")}
            | {"text": (a.get("text") or "")[:800]})


def search_rebuttal(session, ticker, rebuttal, user_id=None):
    '''Search the web for what the user just claimed, and add new results to the session's sources.'''
    results = session.get("sources", [])
    query = f'{ticker} {" ".join(rebuttal.split())}'[:150]
    found = gather_search_results(ticker, [query], results)
    if not found:
        return results, []
    for a in found:
        a["id"] = next_source_id(results + found[:found.index(a)])
    found, _ = label_sentiments(ticker, found, session.get("_id"), user_id)
    db.sessions.update_one({"_id": session["_id"]},
                           {"$push": {"sources": {"$each": [session_source(a) for a in found]}}})
    return results + found, found


def rebuttal_reply(user_id, objection_id, rebuttal):
    '''The devil's advocate answers the user's rebuttal, using the session's sources plus a web
    search on the user's claim.'''
    objection = user.get_objection(user_id, objection_id)
    session = db.sessions.find_one({"_id": objection["sessionId"]}) or {}
    results, new_sources = session.get("sources", []), []
    if session:
        results, new_sources = search_rebuttal(session, objection["ticker"], rebuttal, user_id)
    facts = session.get("facts") or []
    history = "\n".join(f'User: {e["rebuttal"]}\nYou: {e["reply"]}' for e in objection.get("exchanges", []))
    prompt = f'''Stock: {objection["ticker"]}
Your objection ({objection["category"]}): {objection["claim"]}
{history}
User's rebuttal: {rebuttal}

Financial facts:
{format_facts(facts)}

Sources:
{format_sources(results)}

Reply with JSON only:
{{"verdict": "concede|hold", "reply": "2-3 sentences", "sources": ["S1"]}}'''
    result = call_agent_json(prompt, REBUTTAL_SYSTEM, session_id=objection["sessionId"],
                             user_id=user_id, role="rebuttal")
    verdict = str(result.get("verdict") or "").strip().lower()
    result["verdict"] = verdict if verdict in ("concede", "hold") else "hold"
    result["reply"] = str(result.get("reply") or "").strip() or "I couldn't form a reply to that."
    result = check_citations([result], results, facts=facts)[0]
    result["newSources"] = new_sources
    user.record_response(user_id, objection_id, "rebut", rebuttal)
    user.add_exchange(user_id, objection_id, rebuttal, result["reply"])
    return result


# ---------- Check-in (revisit a past thesis) ----------

CHANGES = ["stronger", "weaker", "unchanged", "no_news"]
CHANGE_SYNONYMS = {"strengthened": "stronger", "supported": "stronger", "weakened": "weaker",
                   "contradicted": "weaker", "same": "unchanged", "nonews": "no_news", "none": "no_news"}
CHECKIN_NEWS = 3

CHECKIN_SYSTEM = ("You are the devil's advocate checking in on an investor's earlier thesis. Using only the "
                  "news published since then, say for each of their assumptions whether it now looks "
                  "stronger, weaker or unchanged, or no_news if nothing new covers it. Be honest when "
                  "the news helps them. Cite source ids. Reply with JSON only.")


def share_price(facts):
    return next((f["value"] for f in facts or [] if f["label"] == "Share price"), None)


def checkin_assumptions(thesis):
    '''The thesis's assumptions with a search query each (older theses stored none; the reason stands in).'''
    ticker = thesis.get("ticker") or thesis["stock"]
    items = [{"text": a.get("text"), "query": a.get("query"), "status": a.get("status")}
             for a in thesis.get("assumptions") or []]
    assumptions = make_assumptions(ticker, items or [thesis.get("reason") or f"{ticker} outlook"])
    for a, item in zip(assumptions, items):
        a["statusThen"] = item.get("status")
    return assumptions


def checkin_one(user_id, thesis):
    ticker = thesis.get("ticker") or thesis["stock"]
    session = db.sessions.find_one({"_id": thesis["sessionId"]}) or {}
    since = thesis["createdAt"].date().isoformat()
    assumptions = checkin_assumptions(thesis)
    # Only news the user hasn't seen: not in the analysis, not in an earlier check-in on this thesis
    earlier = list(db.checkins.find({"thesisId": thesis["_id"], "userId": user_id}).sort("createdAt", 1))
    seen = (session.get("sources") or []) + [s for c in earlier for s in c.get("sources") or []]
    news_from = earlier[-1]["createdAt"].date().isoformat() if earlier else since

    # Finnhub since the last look, plus a web search per assumption
    _, news = gather_top_n_news(ticker, CHECKIN_NEWS, start=news_from, ticker=ticker)
    seen_urls = {s["url"] for s in seen}
    articles = [a for a in news if a["url"] not in seen_urls]
    found = gather_search_results(ticker, [a["query"] for a in assumptions], seen + articles)
    articles += [a for a in found if not a.get("date") or a["date"] == "undated" or a["date"] >= news_from]
    for i, a in enumerate(articles):
        a["id"] = f"S{i + 1}"
    results, _ = label_sentiments(ticker, articles, user_id=user_id)

    facts = financials.get_facts(ticker)
    price_then, price_now = share_price(session.get("facts")), share_price(facts)
    if not visible(results):
        checked = [{**a, "change": "no_news", "note": "", "sources": [], "unverified": False} for a in assumptions]
        summary = f"No new news on {ticker} since {news_from}."
    else:
        prompt = f'''Stock: {ticker}
On {since} the user said: "{thesis.get("reason") or "no reason given"}"
Their decision then: {(thesis.get("decision") or "none").replace("_", " ")}
Their assumptions and how the evidence stood then:
{chr(10).join(f'{a["id"]}: {a["text"]} (then: {(a.get("statusThen") or "unchecked").replace("_", " ")})' for a in assumptions)}

Financial facts now:
{format_facts(facts)}

News since {news_from}:
{format_sources(results)}

Reply with JSON only:
{{"assumptions": [{{"id": "A1", "change": "stronger|weaker|unchanged|no_news", "note": "one sentence", "sources": ["S1"]}}],
  "summary": "two sentences: what changed and whether the thesis still holds"}}'''
        result = call_agent_json(prompt, CHECKIN_SYSTEM, user_id=user_id, role="checkin")
        by_id = {}
        for r in as_list(result.get("assumptions")):
            ids = re.findall(r"A\s*(\d+)", str(r.get("id") or ""), re.I) if isinstance(r, dict) else []
            if ids:
                by_id.setdefault(f"A{ids[0]}", r)
        checked = []
        for a in assumptions:
            r = by_id.get(a["id"], {})
            change = re.sub(r"[^a-z_]", "", str(r.get("change") or "").lower())
            change = CHANGE_SYNONYMS.get(change.replace("_", ""), change)
            checked.append({**a, "change": change if change in CHANGES else "no_news",
                            "note": str(r.get("note") or "").strip(), "sources": r.get("sources")})
        checked = check_citations(checked, results, facts=facts)
        summary = str(result.get("summary") or "").strip()

    checkin = {"userId": user_id, "thesisId": thesis["_id"], "sessionId": thesis["sessionId"], "ticker": ticker,
               "since": since, "reason": thesis.get("reason"), "decision": thesis.get("decision"),
               "sources": [session_source(a) for a in results], "assumptions": checked, "summary": summary,
               "priceThen": price_then, "priceNow": price_now, "createdAt": db.now()}
    # The user may have forgotten this thesis or turned memory off while we were working
    if not user.memory_on(user_id) or not db.theses.find_one({"_id": thesis["_id"], "userId": user_id}):
        return None
    checkin["_id"] = db.checkins.insert_one(checkin).inserted_id
    return checkin


def thesis_checkin(user_id, stock=None, limit=3):
    '''Revisit the user's latest thesis on each of their recent stocks (or just `stock`).'''
    if not user.memory_on(user_id):
        raise ValueError("Memory is off, so there are no past theses to check in on. Turn it on in /memory.")
    ticker = find_company(stock) if stock else None
    theses = user.latest_theses(user_id, ticker, limit)
    if not theses:
        raise ValueError(f"No past analysis of {ticker} to check in on." if ticker
                         else "No past analyses to check in on yet. Send me a stock first.")
    checkins, error = [], None
    for t in theses:   # one stock failing (e.g. a Finnhub error) doesn't lose the others
        try:
            c = checkin_one(user_id, t)
        except Exception as e:
            log.exception("Check-in on %s failed", t.get("ticker"))
            error = e
            continue
        if c:
            checkins.append(c)
    if error and not checkins:
        raise error
    return checkins


# ---------- Full run ----------

def lookup_stock(stock):
    '''Ticker, fact table and peers for what the user typed. Raises ValueError if no company is found.
    The bot calls this first to show a snapshot; run_analysis reuses the cached result.'''
    ticker = find_company(stock)
    return {"ticker": ticker, "facts": financials.get_facts(ticker), "peers": financials.get_peers(ticker)}


def run_analysis(user_id, stock, reason=None, stock_risk=None, general_risk=None, intent=None,
                 n=10, start=None, end=None, assumptions=None, ticker=None):
    '''assumptions: confirmed by the user ([{text, query}] or strings). None means extract them here.'''
    profile = user.upsert_user(user_id, general_risk)
    general_risk = general_risk or profile.get("generalRisk")
    remember = profile.get("memoryOn", True)

    session_id = db.sessions.insert_one({"userId": user_id, "stock": stock, "reason": reason,
                                         "intent": intent, "stockRisk": stock_risk,
                                         "generalRisk": general_risk, "remembered": remember,
                                         "status": "running", "createdAt": db.now()}).inserted_id
    log = {"session_id": session_id, "user_id": user_id}

    try:
        stock_info = lookup_stock(ticker or stock)
        ticker, facts, peers = stock_info["ticker"], stock_info["facts"], stock_info["peers"]
        if assumptions is None:
            assumptions = extract_assumptions(ticker, reason, intent, user_id)
        else:
            assumptions = make_assumptions(ticker, assumptions)
        ticker, results, counts = get_news_sentiments(stock, n, start, end,
                                                      queries=[a["query"] for a in assumptions],
                                                      ticker=ticker, **log)
        # Memory only holds past runs; this run's thesis is saved once the run succeeds.
        # With memory off, nothing about the user's past is used.
        memory = user.memory_context(user_id, ticker, peers) if remember else None
        blind_spots = user.get_blind_spots(user_id) if remember else {}
        summary = researcher_summary(ticker, results, counts, facts, role="researcher", **log)
        critique = critic(ticker, results, summary, reason, stock_risk, general_risk,
                          intent, memory, assumptions, facts, role="critic", **log)
        assumptions = critique["assumptions"]
        devil = devils_advocate(ticker, results, summary, reason, stock_risk, general_risk,
                                blind_spots, intent, memory, assumptions, facts, role="devil", **log)

        # The user may have run /forget while this was running; keep nothing in that case
        if not db.sessions.find_one({"_id": session_id}, {"_id": 1}):
            raise ValueError("Your data was deleted while this analysis was running, so nothing was kept.")
        # They may also have turned memory off mid-run: the stricter setting wins
        remember = remember and user.memory_on(user_id)

        user.save_thesis(user_id, session_id, stock, reason, intent, stock_risk, ticker,
                         assumptions=assumptions, peers=peers, remembered=remember)
        for o in devil:
            o["_id"] = db.objections.insert_one({**o, "sessionId": session_id, "userId": user_id,
                                                 "ticker": ticker, "response": None, "rebuttal": None,
                                                 "remembered": remember,
                                                 "createdAt": db.now()}).inserted_id

        done = db.sessions.update_one({"_id": session_id},
                               {"$set": {"ticker": ticker,
                                         # Kept so rebuttals can be argued against the same evidence
                                         "sources": [session_source(a) for a in results],
                                         "facts": facts, "assumptions": assumptions,
                                         "counts": counts, "summary": summary, "critique": critique,
                                         "textSources": dict(Counter(a["textSource"] for a in results)),
                                         "objectionIds": [o["_id"] for o in devil],
                                         "remembered": remember,
                                         "status": "done", "finishedAt": db.now()}})
        if done.matched_count == 0:
            raise ValueError("Your data was deleted while this analysis was running, so nothing was kept.")
    except Exception as e:
        # Don't leave half a run behind in the user's memory
        db.theses.delete_many({"sessionId": session_id})
        db.objections.delete_many({"sessionId": session_id})
        failed = db.sessions.update_one({"_id": session_id},
                                        {"$set": {"status": "error", "error": f"{type(e).__name__}: {e}",
                                                  "finishedAt": db.now()}})
        if failed.matched_count == 0:
            # Session was deleted by /forget mid-run: drop the LLM logs written since
            db.messages.delete_many({"sessionId": session_id})
        raise

    return {"sessionId": session_id, "ticker": ticker, "sources": results, "counts": counts,
            "facts": facts, "assumptions": assumptions, "peers": peers,
            "summary": summary, "critique": critique, "objections": devil,
            "blindSpots": blind_spots, "memory": memory, "remembered": remember}


if __name__ == "__main__":
    out = run_analysis("demo-user", "apple", reason="iPhone duo is getting negative news",
                       stock_risk="medium", general_risk="low", intent="signal", n=5)
    print(json.dumps(out, indent=2, default=str))
