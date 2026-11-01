'''
Agents
- get_news_sentiments : label each news article bullish / bearish / neutral / irrelevant
- researcher_summary  : sourced summary of the news, citing [S#] ids
- critic              : pokes holes in the user's reason and the researcher's summary
- devils_advocate     : argues against buying, user's blind-spot categories first
- rebuttal_reply      : devil's advocate answers when the user argues back
- run_analysis        : runs all of the above for one user and saves to MongoDB
'''

import json
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
import user
from financial_news import find_company, get_company_news

SENTIMENTS = ["bullish", "bearish", "neutral", "irrelevant"]
CATEGORIES = ["valuation", "growth", "margins", "debt", "competition",
              "management", "macro", "sentiment", "other"]
CACHE_HOURS = 6
NEWS_DAYS = 10


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
    end = end or str(date.today())
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
             "$setOnInsert": {"sentiment": None, "reason": None}},
            upsert=True))
    if ops:
        db.news.bulk_write(ops, ordered=False)
    db.news_fetches.update_one({"ticker": ticker, "start": start, "end": end},
                               {"$set": {"fetchedAt": db.now()}}, upsert=True)
    return True


def gather_top_n_news(stock, n=10, start=None, end=None, max_extra=None):
    '''Newest articles until n of them have full text. Teasers and summary-only articles are
    kept for context but don't count towards n. At most n + max_extra articles in total.'''
    ticker = find_company(stock)
    start, end = resolve_dates(start, end)
    fetch_news_to_cache(ticker, start, end)
    max_extra = n if max_extra is None else max_extra

    def candidates():
        seen = set()
        for a in db.news.find({"ticker": ticker, "date": {"$gte": start, "$lte": end}}).sort("datetime", -1):
            if a["headline"] in seen:
                continue
            seen.add(a["headline"])
            yield {"_id": a["_id"],
                   "headline": a["headline"],
                   "summary": a["summary"],
                   "source": a["source"],
                   "url": a["url"],
                   "date": a["date"],
                   "sentiment": a.get("sentiment"),
                   "reason": a.get("reason")}

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
    '''Fill article["text"] with the full article (or paywall teaser), falling back to the Finnhub summary.
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


def get_news_sentiments(stock, n=10, start=None, end=None, session_id=None, user_id=None):
    '''Returns (ticker, results, counts). Sentiments are cached on the news doc and reused by everyone.'''
    ticker, articles = gather_top_n_news(stock, n, start, end)
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
    return ticker, results, counts


# ---------- Helpers shared by the agents ----------

def visible(results):
    '''Sources the agents actually see; irrelevant articles are left out.'''
    return [a for a in results if a["sentiment"] != "irrelevant"]


def format_sources(results):
    return "\n".join(f'{a["id"]}: {a["date"]} | {a["source"]} | {a["sentiment"]} | '
                     f'{a["headline"]} — {a["text"][:800]}'
                     for a in visible(results))


def as_list(value):
    '''LLMs sometimes return null, a single object or a string where a list was asked for.'''
    if isinstance(value, list):
        return value
    if isinstance(value, (dict, str, int)):
        return [value]
    return []


def check_citations(items, results, key="sources"):
    '''Code check: keep only source ids the agent was shown, and flag items left with none.'''
    valid_ids = {a["id"] for a in visible(results)}
    checked = []
    for item in as_list(items):
        if isinstance(item, str):
            item = {"point": item, key: []}
        if not isinstance(item, dict):
            continue
        # Accept "S1", "[S1]", "s1", "S1, S2" or a bare 1
        found = []
        for x in as_list(item.get(key)):
            ids = [f"S{x}"] if isinstance(x, int) else [f"S{n}" for n in re.findall(r"S\s*(\d+)", str(x), re.I)]
            found += [s for s in ids if s in valid_ids]
        item[key] = list(dict.fromkeys(found))
        item["unverified"] = not item[key]
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


# ---------- Researcher ----------

RESEARCHER_SYSTEM = ("You are an equity researcher. Summarise only what the sources say. "
                     "Every key point must cite source ids like S1. Never invent facts, numbers "
                     "or sources. Do not give buy or sell advice. Reply with JSON only.")


def researcher_summary(ticker, results, counts, **log):
    prompt = f'''Stock: {ticker}
Sentiment counts: {json.dumps(counts)}

Sources:
{format_sources(results)}

Write a short, balanced summary of the recent news for {ticker}.
Reply with JSON only:
{{"summary": "3-5 sentences",
  "key_points": [{{"point": "one sentence", "sources": ["S1"]}}],
  "overall_sentiment": "bullish|bearish|neutral|mixed"}}'''
    result = call_agent_json(prompt, RESEARCHER_SYSTEM, **log)
    result["key_points"] = check_citations(result.get("key_points"), results)
    return result


# ---------- Critic ----------

CRITIC_SYSTEM = ("You are a strict reviewer. You did not write the summary and your job is to "
                 "find problems in reasoning, not to argue a side. Check whether claims are "
                 "supported by the cited sources, whether dates or periods are mixed up, and "
                 "whether the user's reason is actually backed by the evidence. Reply with JSON only.")


def critic(ticker, results, summary, reason, stock_risk, general_risk, intent=None, memory=None, **log):
    prompt = f'''Stock: {ticker}
{user_context(reason, stock_risk, general_risk, intent, memory)}

Researcher's summary:
{json.dumps(summary, indent=2)}

Sources:
{format_sources(results)}

List the problems you find. target is "user" if it's about the user's reason, "researcher" if it's about the summary.
Reply with JSON only:
{{"issues": [{{"target": "user|researcher", "issue": "one sentence", "sources": ["S2"]}}],
  "verdict": "one sentence on how well-supported the user's reason is"}}'''
    result = call_agent_json(prompt, CRITIC_SYSTEM, **log)
    issues = [i for i in as_list(result.get("issues")) if isinstance(i, dict) and i.get("issue")]
    for i in issues:
        target = str(i.get("target") or "").strip().lower()
        i["target"] = target if target in ("user", "researcher") else "researcher"
    result["issues"] = check_citations(issues, results)
    return result


# ---------- Devil's advocate ----------

DEVIL_SYSTEM = ("You are a devil's advocate. The user is leaning towards this stock; argue the "
                "strongest case against it using only the sources given. Weigh each risk "
                "against the user's risk appetite. Every objection must cite source ids. "
                "Reply with JSON only.")


def devils_advocate(ticker, results, summary, reason, stock_risk, general_risk, blind_spots,
                    intent=None, memory=None, **log):
    spots = [c for c, v in blind_spots.items() if v["dismissed"] > 0]
    spot_text = (f"The user has often dismissed these kinds of points before: {', '.join(spots)}. "
                 "Include an objection for these if the sources support one."
                 if spots else "No history of dismissed points yet.")
    prompt = f'''Stock: {ticker}
{user_context(reason, stock_risk, general_risk, intent, memory)}
{spot_text}

Researcher's summary:
{json.dumps(summary, indent=2)}

Sources:
{format_sources(results)}

Give 3-5 objections against the user's reason. category must be one of: {", ".join(CATEGORIES)}.
Reply with JSON only:
{{"objections": [{{"category": "valuation", "claim": "one or two sentences", "sources": ["S3"]}}]}}'''
    result = call_agent_json(prompt, DEVIL_SYSTEM, **log)
    items = [o for o in as_list(result.get("objections")) if isinstance(o, dict) and o.get("claim")]
    for o in items:
        category = str(o.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            category = next((c for c in CATEGORIES if c != "other" and c in category), "other")
        o["category"] = category
    items = check_citations(items, results)

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
                   "your objections. Judge their rebuttal honestly: concede if it holds up against the "
                   "sources, hold your ground if it doesn't, and say why in two or three sentences. "
                   "Cite source ids. Reply with JSON only.")


def rebuttal_reply(user_id, objection_id, rebuttal):
    '''The devil's advocate answers the user's rebuttal, using the sources from that session.'''
    objection = user.get_objection(user_id, objection_id)
    session = db.sessions.find_one({"_id": objection["sessionId"]}) or {}
    results = session.get("sources", [])
    history = "\n".join(f'User: {e["rebuttal"]}\nYou: {e["reply"]}' for e in objection.get("exchanges", []))
    prompt = f'''Stock: {objection["ticker"]}
Your objection ({objection["category"]}): {objection["claim"]}
{history}
User's rebuttal: {rebuttal}

Sources:
{format_sources(results)}

Reply with JSON only:
{{"verdict": "concede|hold", "reply": "2-3 sentences", "sources": ["S1"]}}'''
    result = call_agent_json(prompt, REBUTTAL_SYSTEM, session_id=objection["sessionId"],
                             user_id=user_id, role="rebuttal")
    verdict = str(result.get("verdict") or "").strip().lower()
    result["verdict"] = verdict if verdict in ("concede", "hold") else "hold"
    result["reply"] = str(result.get("reply") or "").strip() or "I couldn't form a reply to that."
    result = check_citations([result], results)[0]
    user.record_response(user_id, objection_id, "rebut", rebuttal)
    user.add_exchange(user_id, objection_id, rebuttal, result["reply"])
    return result


# ---------- Full run ----------

def run_analysis(user_id, stock, reason=None, stock_risk=None, general_risk=None, intent=None,
                 n=10, start=None, end=None):
    profile = user.upsert_user(user_id, general_risk)
    general_risk = general_risk or profile.get("generalRisk")

    session_id = db.sessions.insert_one({"userId": user_id, "stock": stock, "reason": reason,
                                         "intent": intent, "stockRisk": stock_risk,
                                         "generalRisk": general_risk,
                                         "status": "running", "createdAt": db.now()}).inserted_id
    log = {"session_id": session_id, "user_id": user_id}

    try:
        ticker, results, counts = get_news_sentiments(stock, n, start, end, **log)
        # Memory only holds past runs; this run's thesis is saved once the run succeeds
        memory = user.memory_context(user_id, ticker)
        summary = researcher_summary(ticker, results, counts, role="researcher", **log)
        critique = critic(ticker, results, summary, reason, stock_risk, general_risk,
                          intent, memory, role="critic", **log)
        blind_spots = user.get_blind_spots(user_id)
        devil = devils_advocate(ticker, results, summary, reason, stock_risk, general_risk,
                                blind_spots, intent, memory, role="devil", **log)

        # The user may have run /forget while this was running; keep nothing in that case
        if not db.sessions.find_one({"_id": session_id}, {"_id": 1}):
            raise ValueError("Your data was deleted while this analysis was running, so nothing was kept.")

        user.save_thesis(user_id, session_id, stock, reason, intent, stock_risk, ticker)
        for o in devil:
            o["_id"] = db.objections.insert_one({**o, "sessionId": session_id, "userId": user_id,
                                                 "ticker": ticker, "response": None,
                                                 "rebuttal": None, "createdAt": db.now()}).inserted_id

        done = db.sessions.update_one({"_id": session_id},
                               {"$set": {"ticker": ticker,
                                         # Kept so rebuttals can be argued against the same sources
                                         "sources": [{k: a[k] for k in ("id", "url", "headline", "source", "date",
                                                                        "sentiment", "textSource")}
                                                     | {"text": a["text"][:800]} for a in results],
                                         "counts": counts, "summary": summary, "critique": critique,
                                         "textSources": dict(Counter(a["textSource"] for a in results)),
                                         "objectionIds": [o["_id"] for o in devil],
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
            "summary": summary, "critique": critique, "objections": devil,
            "blindSpots": blind_spots, "memory": memory}


if __name__ == "__main__":
    out = run_analysis("demo-user", "apple", reason="iPhone duo is getting negative news",
                       stock_risk="medium", general_risk="low", intent="signal", n=5)
    print(json.dumps(out, indent=2, default=str))
