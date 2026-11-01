'''
Basic financials as a fact table.

Each number gets an id (F1, F2...) so agents can cite it, and code can check that a number
written next to an F# citation actually matches the fact.
'''

import re
from datetime import timedelta, timezone

import db
from financial_news import get_company_basic_financials, get_company_peers, get_quote

FACTS_HOURS = 12
PEERS_DAYS = 7
MAX_PEERS = 10

# (label, Finnhub metric keys tried in order, unit, period)
FACT_FIELDS = [
    ("P/E ratio", ["peTTM", "peBasicExclExtraTTM", "peExclExtraTTM"], "x", "TTM"),
    ("Price/sales", ["psTTM"], "x", "TTM"),
    ("Revenue growth", ["revenueGrowthTTMYoy"], "%", "TTM vs prior year"),
    ("Revenue growth, latest quarter", ["revenueGrowthQuarterlyYoy"], "%", "latest quarter vs a year ago"),
    ("EPS growth", ["epsGrowthTTMYoy"], "%", "TTM vs prior year"),
    ("Gross margin", ["grossMarginTTM"], "%", "TTM"),
    ("Operating margin", ["operatingMarginTTM"], "%", "TTM"),
    ("Net margin", ["netProfitMarginTTM"], "%", "TTM"),
    ("Return on equity", ["roeTTM"], "%", "TTM"),
    ("Debt/equity", ["totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual"], "x", "latest quarter"),
    ("Current ratio", ["currentRatioQuarterly", "currentRatioAnnual"], "x", "latest quarter"),
    ("Dividend yield", ["dividendYieldIndicatedAnnual", "currentDividendYieldTTM"], "%", "annual"),
    ("Beta", ["beta"], "", "5 years"),
    ("52-week high", ["52WeekHigh"], "$", "52 weeks"),
    ("52-week low", ["52WeekLow"], "$", "52 weeks"),
    ("Market cap", ["marketCapitalization"], "$M", "latest"),
]

# Facts the Telegram snapshot shows when the user picks a stock
SNAPSHOT = ["P/E ratio", "Revenue growth", "Net margin", "Debt/equity", "52-week high", "52-week low"]


def build_facts(metrics, quote):
    facts = []
    if quote and quote.get("c"):
        facts.append({"label": "Share price", "value": float(quote["c"]), "unit": "$", "period": "latest"})
        if quote.get("dp") is not None:
            facts.append({"label": "Price change today", "value": float(quote["dp"]), "unit": "%", "period": "today"})
    for label, keys, unit, period in FACT_FIELDS:
        value = next((metrics[k] for k in keys if isinstance(metrics.get(k), (int, float))), None)
        if value is not None:
            facts.append({"label": label, "value": float(value), "unit": unit, "period": period})
    for i, f in enumerate(facts):
        f["id"] = f"F{i + 1}"
    return facts


def get_facts(ticker):
    '''Fact table for a ticker, cached in Mongo for FACTS_HOURS and shared by everyone.'''
    cached = db.financials.find_one({"ticker": ticker})
    if cached:
        fetched = cached["fetchedAt"]
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if db.now() - fetched < timedelta(hours=FACTS_HOURS):
            return cached["facts"]

    try:
        metrics = (get_company_basic_financials(ticker) or {}).get("metric") or {}
        quote = get_quote(ticker) or {}
    except Exception:
        # Never fatal: an old table, or none (the agents then work from news only). Not cached,
        # so the next run tries again
        return cached["facts"] if cached else []
    facts = build_facts(metrics, quote)
    db.financials.update_one({"ticker": ticker},
                             {"$set": {"facts": facts, "fetchedAt": db.now()}}, upsert=True)
    return facts


def get_peers(ticker):
    '''Similar companies (Finnhub sub-industry peers), cached in Mongo. Never fatal: [] on failure.'''
    cached = db.companies.find_one({"ticker": ticker})
    if cached:
        fetched = cached["fetchedAt"]
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if db.now() - fetched < timedelta(days=PEERS_DAYS):
            return cached["peers"]
    try:
        peers = [p for p in get_company_peers(ticker) or [] if isinstance(p, str) and p != ticker][:MAX_PEERS]
    except Exception:
        return cached["peers"] if cached else []
    db.companies.update_one({"ticker": ticker}, {"$set": {"peers": peers, "fetchedAt": db.now()}}, upsert=True)
    return peers


def format_value(fact):
    v, unit = fact["value"], fact["unit"]
    if unit == "$M":
        return f"${v / 1e6:.2f}T" if v >= 1e6 else f"${v / 1e3:.1f}B" if v >= 1e3 else f"${v:.0f}M"
    if unit == "$":
        return f"${v:,.2f}"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "x":
        return f"{v:.2f}x"
    return f"{v:.2f}"


def format_facts(facts):
    return "\n".join(f'{f["id"]}: {f["label"]} ({f["period"]}) = {format_value(f)}' for f in facts)


# ---------- Number checking ----------

SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6,
         "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12, "tn": 1e12, "t": 1e12}
_AMOUNT = r"(\d[\d,]*(?:\.\d+)?)"
_SUFFIX = r"(?:\s*(%|x\b|thousand|million|billion|trillion|bn|mn|tn|[kmbt]\b))?"
# A number not glued to a letter or dot, so ids and periods (S1, F2, Q3, FY2025) are never amounts.
# A minus only counts as a sign when it isn't joining two numbers ("34-36x" is a range)
NUMBER = re.compile(r"(?<![\w.])(-)?" + _AMOUNT + _SUFFIX, re.I)
RANGE = re.compile(r"(?<![\w.])" + _AMOUNT + r"\s*(?:-|to)\s*" + _AMOUNT + _SUFFIX, re.I)
# Years, but not prices like $2050.12 or amounts like 2024%
YEAR = re.compile(r"(?<![$\d.,])\b(?:19|20)\d{2}\b(?!\s*%|\s*x\b|[.,]\d)", re.I)
DASHES = str.maketrans({"−": "-", "–": "-", "—": "-"})


def _clean(text):
    return YEAR.sub(" ", str(text or "").translate(DASHES))


def _interval(raw, suffix, sign=1):
    '''What a written number can stand for: "24%" is anything that rounds to 24 (23.5 to 24.5),
    "24.3%" is 24.25 to 24.35.'''
    value = float(raw.replace(",", ""))
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    scale = SCALE.get((suffix or "").lower(), 1)
    half = 0.5 * 10 ** -decimals * scale
    return sign * value * scale - half, sign * value * scale + half


def extract_numbers(text):
    '''Numbers in a sentence, with their scale applied: "$3.5 trillion" -> 3.5e12, "25%" -> 25.
    Citations, quarters and years are ignored.'''
    found = []
    for sign, raw, suffix in NUMBER.findall(_clean(text)):
        found.append((-1 if sign else 1) * float(raw.replace(",", "")) * SCALE.get(suffix.lower(), 1))
    return found


def amounts(text):
    '''Intervals each written number or range could mean.'''
    text = _clean(text)
    spans = [_interval(raw, suffix, -1 if sign else 1) for sign, raw, suffix in NUMBER.findall(text)]
    for low, high, suffix in RANGE.findall(text):
        spans.append((_interval(low, suffix)[0], _interval(high, suffix)[1]))
    return spans


def matches(span, fact):
    '''The fact, as written to the agents, falls in what the number could mean.'''
    v = fact["value"] * (1e6 if fact["unit"] == "$M" else 1)   # Finnhub reports market cap in millions
    eps = 1e-9 * max(1.0, abs(v))
    return span[0] - eps <= v <= span[1] + eps


DOWN_WORDS = re.compile(r"\b(fell|fall|falls|falling|declin\w*|drop\w*|shr[aui]nk\w*|down|negative|"
                        r"contract\w*|lower|loss\w*|slump\w*|decreas\w*)\b", re.I)


def check_numbers(text, fact_ids, facts):
    '''"verified" if every cited fact has a matching number in the text, "mismatch" if the text has
    numbers but a cited fact matches none of them, None if there's nothing to check.'''
    cited = [f for f in facts or [] if f["id"] in fact_ids]
    spans = amounts(text)
    if not cited or not spans:
        return None
    # "revenue shrank 1.2%" means -1.2
    if DOWN_WORDS.search(str(text)):
        spans += [(-high, -low) for low, high in spans if low > 0]
    return "verified" if all(any(matches(span, f) for span in spans) for f in cited) else "mismatch"
