'''
User pipeline: profile, responses to objections, and user-related memory.

- Profile     : general risk appetite, muted categories, whether memory is on
- Theses      : why the user looked at each stock (reason, intent, risk for that stock) and their decision after
- Responses   : accept / rebut / dismiss on each devil's-advocate objection, plus the back-and-forth on rebuttals
- Blind spots : per-category dismiss counts, used to rank future objections
- Memory      : a short text summary of all of the above that goes into the agents' prompts,
                including theses on similar stocks (Finnhub peers)

With memory off, runs are still stored (so buttons work) but marked remembered: False, and
nothing from them is used in later runs or blind spots. Users can delete single theses.
'''

from bson import ObjectId
from bson.errors import InvalidId

import db

RISK_LEVELS = ["low", "medium", "high"]
INTENTS = ["curious", "signal"]
RESPONSES = ["accept", "rebut", "dismiss"]
DECISIONS = ["still_keen", "less_sure", "dropping"]

# Old docs have no "remembered" field and count as remembered
REMEMBERED = {"remembered": {"$ne": False}}


def to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except InvalidId:
        raise ValueError(f"Not a valid id: {value}")


def clean_choice(value, allowed, name):
    value = str(value or "").strip().lower()
    if value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")
    return value


# ---------- Profile ----------

def upsert_user(user_id, general_risk=None, name=None):
    update = {"$setOnInsert": {"createdAt": db.now(), "mutedCategories": [], "memoryOn": True},
              "$set": {"updatedAt": db.now()}}
    if general_risk is not None:
        update["$set"]["generalRisk"] = clean_choice(general_risk, RISK_LEVELS, "general_risk")
    if name is not None:
        update["$set"]["name"] = name
    db.users.update_one({"userId": user_id}, update, upsert=True)
    return db.users.find_one({"userId": user_id})


def get_user(user_id):
    return db.users.find_one({"userId": user_id})


def set_memory(user_id, on):
    '''Turn memory on or off. Off: later runs don't use or add to what we remember.'''
    db.users.update_one({"userId": user_id},
                        {"$set": {"memoryOn": bool(on), "updatedAt": db.now()},
                         "$setOnInsert": {"createdAt": db.now(), "mutedCategories": []}}, upsert=True)


def memory_on(user_id):
    return (get_user(user_id) or {}).get("memoryOn", True)


def mute_category(user_id, category, mute=True):
    op = "$addToSet" if mute else "$pull"
    db.users.update_one({"userId": user_id}, {op: {"mutedCategories": category.strip().lower()}})


# ---------- Theses (why the user looked at a stock) ----------

def save_thesis(user_id, session_id, stock, reason=None, intent=None, stock_risk=None, ticker=None,
                assumptions=None, peers=None, remembered=True):
    thesis = {"userId": user_id, "sessionId": session_id, "stock": stock, "ticker": ticker,
              "reason": reason,
              "intent": clean_choice(intent, INTENTS, "intent") if intent else None,
              "stockRisk": clean_choice(stock_risk, RISK_LEVELS, "stock_risk") if stock_risk else None,
              "assumptions": [{k: a.get(k) for k in ("id", "text", "status", "note")} for a in assumptions or []],
              "peers": list(peers or []), "remembered": bool(remembered),
              "decision": None, "createdAt": db.now()}
    thesis["_id"] = db.theses.insert_one(thesis).inserted_id
    return thesis


def record_decision(user_id, session_id, decision):
    '''What the user thinks after seeing the case against: still_keen, less_sure or dropping.'''
    decision = clean_choice(decision, DECISIONS, "decision")
    res = db.theses.update_one({"sessionId": to_object_id(session_id), "userId": user_id},
                               {"$set": {"decision": decision, "decidedAt": db.now()}})
    if res.matched_count == 0:
        raise ValueError("No thesis for this session")


def past_theses(user_id, ticker=None, limit=5, peers=None, include_forgotten=False):
    '''Most recent theses. With a ticker: theses on it and on similar stocks, i.e. a past ticker is
    in this stock's peers, or this ticker is in that thesis's peers (so older rows still match).'''
    query = {"userId": user_id}
    if not include_forgotten:
        query |= REMEMBERED
    if ticker:
        query["$or"] = [{"ticker": ticker}, {"ticker": {"$in": list(peers or [])}}, {"peers": ticker}]
    return list(db.theses.find(query).sort("createdAt", -1).limit(limit))


def delete_thesis(user_id, thesis_id):
    '''Forget one run: the thesis, its objections (so blind spots drop them), session and LLM logs.'''
    thesis = db.theses.find_one({"_id": to_object_id(thesis_id), "userId": user_id})
    if not thesis:
        raise ValueError("Thesis not found")
    session_id = thesis["sessionId"]
    db.objections.delete_many({"sessionId": session_id, "userId": user_id})
    db.messages.delete_many({"sessionId": session_id})
    db.sessions.delete_one({"_id": session_id, "userId": user_id})
    db.theses.delete_one({"_id": thesis["_id"]})
    return thesis


# ---------- Responses to objections ----------

def get_objection(user_id, objection_id):
    obj = db.objections.find_one({"_id": to_object_id(objection_id), "userId": user_id})
    if not obj:
        raise ValueError("Objection not found")
    return obj


def record_response(user_id, objection_id, response, rebuttal=None):
    '''accept / rebut / dismiss. Only the owner can respond. This feeds the blind-spot profile.'''
    response = clean_choice(response, RESPONSES, "response")
    res = db.objections.update_one({"_id": to_object_id(objection_id), "userId": user_id},
                                   {"$set": {"response": response, "rebuttal": rebuttal,
                                             "respondedAt": db.now()}})
    if res.matched_count == 0:
        raise ValueError("Objection not found")


def add_exchange(user_id, objection_id, rebuttal, reply):
    '''Keep the back-and-forth when the user argues with an objection.'''
    db.objections.update_one({"_id": to_object_id(objection_id), "userId": user_id},
                             {"$push": {"exchanges": {"rebuttal": rebuttal, "reply": reply,
                                                      "createdAt": db.now()}}})


# ---------- Blind spots ----------

def get_blind_spots(user_id, include_muted=False):
    '''Dismiss counts per category, most dismissed first. Muted categories are left out
    unless include_muted is set (for showing the profile).'''
    user = get_user(user_id) or {}
    muted = set(user.get("mutedCategories", []))
    rows = db.objections.aggregate([
        {"$match": {"userId": user_id, "response": {"$ne": None}, **REMEMBERED}},
        {"$group": {"_id": "$category",
                    "dismissed": {"$sum": {"$cond": [{"$eq": ["$response", "dismiss"]}, 1, 0]}},
                    "total": {"$sum": 1}}},
    ])
    spots = {r["_id"]: {"dismissed": r["dismissed"], "total": r["total"], "muted": r["_id"] in muted}
             for r in rows if include_muted or r["_id"] not in muted}
    return dict(sorted(spots.items(), key=lambda kv: (kv[1]["dismissed"], kv[1]["total"]), reverse=True))


# ---------- Memory ----------

STATUS_ICONS = {"supported": "supported", "contradicted": "contradicted", "mixed": "mixed evidence",
                "no_evidence": "no evidence found"}


def memory_context(user_id, ticker=None, peers=None):
    '''Short text for the agents' prompts: past reasons, how their assumptions held up, decisions,
    and what the user tends to dismiss. With a ticker, only that stock and similar ones.'''
    lines = []
    for t in past_theses(user_id, ticker, limit=3, peers=peers):
        when = t["createdAt"].date().isoformat()
        past = t.get("ticker") or t["stock"]
        similar = " (a similar company)" if ticker and past != ticker else ""
        decided = f', decided: {t["decision"].replace("_", " ")}' if t.get("decision") else ""
        lines.append(f'- {when} {past}{similar}: "{t.get("reason") or "no reason given"}"'
                     f' (risk {t.get("stockRisk") or "?"}{decided})')
        for a in t.get("assumptions") or []:
            if a.get("status"):
                lines.append(f'    assumed "{a["text"]}": {STATUS_ICONS.get(a["status"], a["status"])}')
    spots = [f"{c} ({v['dismissed']}/{v['total']} dismissed)"
             for c, v in get_blind_spots(user_id).items() if v["dismissed"]]
    text = ""
    if lines:
        about = f" on {ticker} and similar stocks" if ticker else ""
        text += f"User's past theses{about}:\n" + "\n".join(lines) + "\n"
    if spots:
        text += "Kinds of points this user tends to dismiss: " + ", ".join(spots) + "\n"
    return text or "No past history for this user."


def get_memory(user_id):
    '''Everything stored about the user, for /memory.'''
    return {"user": get_user(user_id),
            # Memory-off runs are listed too, so they can be seen and deleted
            "theses": past_theses(user_id, limit=10, include_forgotten=True),
            "memoryOn": memory_on(user_id),
            "blindSpots": get_blind_spots(user_id, include_muted=True)}


def forget_user(user_id):
    '''Delete everything stored about this user. Shared news cache is kept.'''
    session_ids = [s["_id"] for s in db.sessions.find({"userId": user_id}, {"_id": 1})]
    counts = {
        "theses": db.theses.delete_many({"userId": user_id}).deleted_count,
        "objections": db.objections.delete_many({"userId": user_id}).deleted_count,
        "messages": db.messages.delete_many({"$or": [{"userId": user_id},
                                                     {"sessionId": {"$in": session_ids}}]}).deleted_count,
        "sessions": db.sessions.delete_many({"userId": user_id}).deleted_count,
        "users": db.users.delete_many({"userId": user_id}).deleted_count,
    }
    return counts
