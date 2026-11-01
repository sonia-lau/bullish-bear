# Bullish Bear: Build Plan

🐂 Bullish by instinct. 🐻 Bear by design.

Proof of concept in Python. Agents run from the terminal (`utils/agent.py`); Telegram (`bot.py`) is a thin layer on top.

**Deadlines:** 2-min demo video due Tue 3 Nov 2026 (Week 11 showcase) · report (max 6 pages) due end of Week 11 (~Sun 8 Nov, confirm)

**Run:** `.venv/bin/python bot.py` (bot) · `cd utils && ../.venv/bin/python agent.py` (one analysis in the terminal) · `.venv/bin/python -m pytest` (tests, needs local Mongo, no internet)

---

## 1. Architecture

```
User (Telegram: bot.py)
   │ stock → 📊 snapshot (price, P/E, growth, margin, debt, 52w range)
   │ → curious or buy/sell signal? → reason
   │ → 🧪 assumptions behind the reason (user confirms or rewrites) → risk appetite for this stock
   ▼
run_analysis (utils/agent.py, plain code orchestrator)
   │ 1. Facts        → Finnhub basic financials + quote as a fact table F1.. (cached 12 h); peers (cached 7 d)
   │ 2. News         → Finnhub news, cached in Mongo, full article text via trafilatura
   │                   (teasers / paywalls don't count towards n; one more article is fetched)
   │ 3. Web search   → Tavily news search per assumption (general sentiment / press), cached 6 h,
   │                   numbered after the Finnhub sources, marked 🔎
   │ 4. Sentiment    → bullish / bearish / neutral / irrelevant per article, cached for everyone
   │ 5. Researcher   → sourced summary citing S# sources and F# facts (ids + numbers checked in code)
   │ 6. Critic       → each assumption: supported / contradicted / mixed / no evidence; other holes
   │                   ("no evidence" ≠ wrong: the critic must not dismiss a claim the sources don't cover)
   │ 7. Devil's adv. → objections by category, user's blind-spot categories first
   ▼
User responds (Telegram buttons)
   │ Accept / Rebut / Dismiss each objection → rebuttal is web-searched → rebuttal_reply concedes or holds
   │ Final decision: still keen / less sure / dropping
   ▼
User memory (utils/user.py, only if memory is on) → past theses on this stock and similar stocks (peers),
   how their assumptions held up, dismiss counts → next run's prompts and objection ranking
   ▼
/checkin (later) → news since the thesis (only articles the user hasn't seen) + a web search per assumption
   → each assumption stronger / weaker / unchanged / no news, price then vs now → "Where do you stand now?"
   ▼
MongoDB (bullish_bear)
```

## 2. Files

| File | What it does |
|---|---|
| `utils/CONSTANTS.py` | Loads `.env` (OpenRouter, Mongo, Finnhub, Telegram keys, model name) |
| `utils/db.py` | Mongo connection, collections, indexes |
| `utils/financial_news.py` | Finnhub wrappers, `find_company` (exact ticker → US common stock → first result), peers |
| `utils/financials.py` | Fact table (F1..), number checking, peers cache |
| `utils/web_search.py` | Tavily news search for the user's claims |
| `utils/agent.py` | LLM calls, news cache, web search cache, article text, sentiment, assumptions, researcher, critic, devil's advocate, rebuttal reply, `run_analysis` |
| `utils/user.py` | Profile, memory on/off, theses, responses, decisions, blind spots, memory context (incl. similar stocks), delete one / forget all |
| `bot.py` | Telegram bot |
| `tests/` | pytest suite (fake LLM, Finnhub, Tavily, Telegram; throwaway Mongo db) |
| `requirements.txt` | Direct dependencies |
| `app.py` | Old Flask placeholder with the original step plan, unused |

## 3. Agents

| Agent | Job | Output |
|---|---|---|
| **Assumptions** | Turn the reason into 1–3 checkable claims + a search query each | `[{id: A#, text, query}]`, user confirms or rewrites |
| **Sentiment** | Label each article for this ticker | `{sentiment, reason}`, cached on the news doc |
| **Researcher** | Summarise only what the sources say | `{summary, key_points[{point, sources}], overall_sentiment}` |
| **Critic** (hole-poker) | Each assumption vs evidence; unsupported claims, wrong periods | `{assumptions[{id, status, note, sources}], issues[{target: user\|researcher, issue, sources}], verdict}` |
| **Devil's advocate** | What's the case against? Weighted by risk appetite, uses user memory | `{objections[{category, claim, sources}]}`, ranked in code by dismiss counts |
| **Rebuttal reply** | Devil's advocate answers when the user argues back; searches the claim first | `{verdict: concede\|hold, reply, sources, newSources}` |

Every agent sees intent, reason, stock risk, general risk, the fact table and (if memory is on) a short memory of the user's past theses on this and similar stocks and their dismissals. Citations are checked in code: ids the agent wasn't shown are dropped and the point is flagged `unverified`. Numbers next to an `F#` citation are compared with the fact: `numberCheck: verified | mismatch`.

Objection categories: `valuation`, `growth`, `margins`, `debt`, `competition`, `management`, `macro`, `sentiment`, `other`.

## 4. MongoDB collections

```
users        { userId: "tg:<id>", name, generalRisk, mutedCategories, memoryOn, createdAt, updatedAt }
news         { ticker, url, headline, summary, source, date, datetime,
               fullText, textSource: "article"|"teaser"|"summary",
               sentiment, reason, sentimentModel, fetchedAt,
               origin: "search" }                                   ← shared cache (origin only on web results)
news_fetches { ticker, start, end, fetchedAt }                       ← 6-hour cache window
searches     { ticker, queryKey (sha256), urls, fetchedAt }          ← 6-hour web search cache (no user words)
financials   { ticker, facts[{id, label, value, unit, period}], fetchedAt }   ← 12-hour cache
companies    { ticker, peers, fetchedAt }                            ← 7-day peers cache
sessions     { userId, stock, ticker, reason, intent, stockRisk, generalRisk, status, remembered,
               sources[{id, url, headline, source, date, sentiment, textSource, origin, query, text}],
               facts, assumptions, counts, textSources, summary, critique, objectionIds, error }
theses       { userId, sessionId, stock, ticker, reason, intent, stockRisk,
               assumptions[{id, text, query, status, note}], peers, remembered, decision }
objections   { sessionId, userId, ticker, category, claim, sources, unverified, numberCheck, whyFirst,
               response: "accept"|"rebut"|"dismiss", rebuttal, exchanges[{rebuttal, reply}], remembered }
checkins     { userId, thesisId, sessionId, ticker, since, reason, sources, summary, priceThen, priceNow,
               assumptions[{id, text, statusThen, change, note, sources}], decision, createdAt }
messages     { sessionId, userId, role, system, prompt, content }   ← every LLM call
```

Blind spots are counted from `objections` (dismissed / responded per category), not stored separately. Runs with memory off are stored with `remembered: false` (so their buttons still work) and ignored by memory and blind spots.

## 5. Checklist

### Setup
- [x] MongoDB running locally (Homebrew), `utils/db.py`
- [x] OpenRouter key and model (`z-ai/glm-5.3-flash`) in `.env`
- [x] Finnhub key in `.env`
- [x] Telegram bot token in `.env` (`TELEGRAM_API_KEY`)
- [x] `python-telegram-bot` 21.11.1, `trafilatura`, `pymongo`, `finnhub-python` in `.venv`
- [x] `requirements.txt`
- [x] Tavily: back, but only for targeted searches on the user's claims (`topic="news"`); Finnhub stays the main feed
- [x] `pytest` suite: 201 tests, ~2.5 s, no network; mutation-checked (re-introducing 19 known bugs each makes it fail)
- [x] Independent review of web search / facts / assumptions / peers / memory control: 21 confirmed issues fixed, each with a test
      (number checker misreading "a 24.3%", ranges, "−"; loose tolerance on small ratios; Finnhub outage killing a run;
      memory-off runs not deletable; user's rebuttal text kept in the shared cache; one user blocking others; broken HTML on long lines)

### Agents (`utils/agent.py`)
- [x] `call_agent` via OpenRouter, retries on 429/5xx, logs every call to `messages`
- [x] `call_agent_json`: parses JSON from fenced or chatty replies, retries once
- [x] `gather_top_n_news`: Finnhub news cached per ticker in Mongo, shared across users
- [x] Full article text via trafilatura, cached; teaser detection; top-up so n full articles are read
- [x] `get_news_sentiments`: per-article label + counts dict, cached for everyone
- [x] Researcher summary with code-checked `S#` citations
- [x] Critic
- [x] Devil's advocate with categories and blind-spot ranking (reason shown)
- [x] `rebuttal_reply`: concede or hold, against the same session's sources
- [x] `run_analysis`: full run, saves everything, marks failed sessions as `error`
- [x] Robust to bad LLM JSON (null lists, `[S1]`/`s1` ids, capitalised categories, synonyms)
- [x] **Fact table** (`financials.py`): price, P/E, growth, margins, debt/equity, 52w range… as `F#`; numbers checked in code
- [x] **Web search for general sentiment**: one Tavily search per assumption, cached; search failures don't stop a run
- [x] **Assumptions**: extracted from the reason, confirmed by the user, each judged supported / contradicted / mixed / no evidence
- [x] **Rebuttals are searched**: the user's claim is looked up so the bot can concede to real evidence
- [x] Prompts: "no evidence" is not "wrong"; agents mustn't dismiss a user claim just because sources are silent

### User pipeline (`utils/user.py`)
- [x] Profile: general risk appetite, muted categories
- [x] Theses: reason, intent (curious / signal), stock risk, decision after the debate
- [x] Responses: accept / rebut / dismiss, owner-only; rebuttal exchanges kept
- [x] Blind spots: dismiss counts per category, mute / unmute
- [x] `memory_context`: past theses on this ticker + dismissed categories, fed into critic and devil's advocate
- [x] `get_memory` / `forget_user` (deletes all of a user's data, keeps the shared news cache)
- [x] **Memory the user controls**: consent at `/start`, on/off in `/memory`; off = not used, not added to
- [x] **Delete one thesis** (its objections, session, LLM logs) from `/memory`
- [x] **Similar stocks**: past theses on Finnhub peers feed memory ("you dropped MSFT for the same reason"), from `app.py` step 2
- [x] Assumptions and how they held up are remembered per thesis
- [x] Thesis saved only when a run succeeds; failed or forgotten runs leave nothing in memory

### Telegram (`bot.py`)
- [x] `/start`: intro, what's stored, asks general risk appetite
- [x] Flow: stock → 📊 snapshot → curious or signal → reason (or `/skip`) → 🧪 assumptions (✅ check / ✏️ rewrite) → stock risk → research in background
- [x] Unknown company answered straight away (lookup before the questions)
- [x] Report: financial facts section, 🔎 web-search sources, "numbers checked ✓" / "number doesn't match" flags
- [x] Assumptions vs evidence section before the critic's issues; rebuttal reply lists what the search found
- [x] Report: overall sentiment, counts, how many read in full, key points with linked `[S#]`, sources list
- [x] Critic message
- [x] Collapsible report: summary, overall and top 3 key points visible; other points, facts and sources in
      tap-to-expand blocks (`<blockquote expandable>`); critic shows the verdict and top 2 issues the same way
- [x] `/checkin [stock]`: revisits the latest thesis per stock (up to 3) against news since then, only unseen
      articles; per-assumption change, price then → now, ends with "Where do you stand now?" (needs memory on)
- [x] Each objection with Fair point / Rebut / Dismiss buttons (`callback_data` ≤ 64 bytes)
- [x] Rebut → user types counter-argument → devil concedes or holds → can rebut again
- [x] "Where do you stand?" buttons → decision saved
- [x] `/memory` (with forget button), `/blindspots` (with mute / unmute), `/risk`, `/forget`, `/cancel`, `/help`
- [x] Expired buttons answered with "This question has expired"; `/cancel` also stops a pending rebuttal
- [x] "Not financial advice" line on every report
- [x] Bot tests through real PTB handler dispatch (in `tests/test_bot.py`): snapshot, assumptions confirm/rewrite, typed reason, /cancel, rebut mid-flow, stale buttons, failed runs, memory toggle, delete one thesis
- [x] Independent review of agents, user pipeline and bot; all confirmed issues fixed
- [ ] **Live test on Telegram** with 2–3 tickers (needs your machine; sandbox has no internet). Check especially:
      Tavily `topic="news"` results look relevant, Finnhub metric keys exist for your tickers (snapshot not empty), peers list

### Evaluation (prof: quantifiable results; user study is a plus)
Every evaluation gives a number, ideally against a baseline. Scripts go in `evals/`, one command each, CSV + summary table.
Needs your machine (live Finnhub, Tavily, OpenRouter).

| # | Evaluation | Ground truth | Metric | Shows design idea |
|---|---|---|---|---|
| 1 | Assumption checking | ~20 claims built from the fact table (e.g. "revenue growing" when growth is −1.2% → contradicted) | accuracy, confusion matrix | verifiable claims |
| 2 | "No evidence ≠ wrong" | true claims about recent press, web search on vs off | % true claims wrongly contradicted | verifiable claims (the iPhone gap) |
| 3 | Rebuttal honesty | rebuttals backed by evidence vs made up | concede rate true vs false (gap = score) | adversarial debate |
| 4 | Grounding | code checks on every run | % unverified, % numbers verified / mismatch; vs one-prompt baseline | verifiable claims |
| 5 | Blind spots | simulated users always dismissing one category vs random | runs until it ranks first; false blind-spot rate | memory |
| 6 | Article fetching | `sessions.textSources` | % full / teaser / summary | — |
| 7 | Cost / speed | logs | seconds, LLM calls, cache speed-up | — |
| 8 | Sentiment | 30 articles hand-labelled | accuracy, Cohen's κ; full text vs summary | — |
| 9 | Citation support | 30 key points hand-checked (+ LLM judge agreement) | % supported | verifiable claims |
| 10 | User study (plus) | 3–5 people, task script | SUS, decision change, blind-spot accuracy (Likert), "did the check-in change your view?", time | all |

- [ ] Build `evals/` (datasets + scripts + tests with fakes)
- [ ] Run 1–7 live, hand-label 8–9, user study 10

### Deliverables
- [ ] Video (2 min): one ticker end to end, show a dismiss changing the next run's order
- [ ] Report (≤ 6 pages): problem, three design ideas + why they work, architecture, evaluation results, limitations

## 6. Timeline

| Day | Date | Work |
|---|---|---|
| Sun | 1 Nov | ✅ Agents, user pipeline, Telegram bot. Live test on Telegram, fix issues. |
| Mon | 2 Nov | Pick 1–2 demo tickers. Script, record and edit the 2-min video. |
| Tue | 3 Nov | **Video due.** |
| Wed | 4 Nov | Run evals 1–7 (scripts), hand-label 8–9. |
| Thu | 5 Nov | User study (10), fill results tables. |
| Fri–Sat | 6–7 Nov | Write report. |
| Sun | 8 Nov | **Report due** (end of Week 11, confirm). |

## 7. Demo script (2 min)

1. `/start` → set risk appetite (5 s)
2. Send a ticker → 📊 snapshot → "buy/sell signal" → type a reason → confirm assumptions → risk appetite (20 s)
3. Report with linked sources (🔎 found for your claim), facts with "numbers checked ✓" (15 s)
4. Assumptions vs evidence + critic (15 s)
5. Devil's advocate objections → dismiss one, rebut another (searched) → bot concedes or holds (30 s)
6. Run a similar stock (a peer) → memory brings up the first thesis; dismissed category comes first (20 s)
7. `/checkin` on the first stock → what changed for each assumption since then (10 s)
8. `/memory` (delete one) and `/blindspots` (5 s)

Tip: run the ticker once before recording so news and sentiment are cached and the demo is fast.

## 8. Risks

- **Free model slow/unreliable:** model name in `.env`, switch in one line.
- **LLM returns bad JSON:** parsed leniently, retried once, shapes normalised in code.
- **Finnhub rate limits:** news cached per ticker for 6 hours; sentiment and article text cached forever.
- **Paywalls:** teaser detection + top-up; fall back to Finnhub summary.
- **Telegram limits:** messages split under 4096 chars, HTML escaped, long runs in background.
- **Tavily irrelevant results:** only used for the user's claims, labelled by the sentiment agent (irrelevant ones are hidden), never fatal.
- **Check-in finds nothing:** says "No new news" without an LLM call; old theses without stored queries search the reason.
- **Regressions as the project grows:** run `.venv/bin/python -m pytest` before each change; add a test with each fix.

## 9. Future work (out of scope for the PoC)

- Web view of the full report (Telegram stays the main interface; the collapsible blocks cover most of the space problem)
- Scheduled daily `/checkin` (needs `python-telegram-bot[job-queue]` and an opt-in per user)
