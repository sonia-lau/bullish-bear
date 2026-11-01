'''
Bullish Bear Telegram bot.

Flow: send a stock → snapshot of its financials → curious or buy/sell signal? → your reason
      → confirm (or rewrite) the assumptions behind it → risk appetite for it
      → sourced report → critic checks each assumption → devil's advocate objections
        (Accept / Rebut / Dismiss; a rebuttal is searched on the web) → where do you stand now?

Commands: /analyse, /checkin, /memory, /blindspots, /risk, /forget, /help, /cancel
Run:      .venv/bin/python bot.py
'''

import asyncio
import html
import logging
import os
import re
import sys
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ForceReply
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)
from telegram.warnings import PTBUserWarning

import CONSTANTS
import agent
import db
import financials
import user

# The conversation mixes text and button steps on purpose; this warning is about per-message tracking
warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bullish-bear")

INTENT, REASON, ASSUMPTIONS, EDIT_ASSUMPTIONS, STOCK_RISK = range(5)
NEWS_COUNT =  10
MAX_MESSAGE = 4000  # Telegram's limit is 4096
DISCLAIMER = "<i>🐻 Not financial advice. The bot argues; you decide.</i>"
HEADLINE_CHARS = 300
KEY_POINTS_SHOWN = 3   # the rest of the report folds away (tap to expand)
ISSUES_SHOWN = 2

SENTIMENT_EMOJI = {"bullish": "🐂", "bearish": "🐻", "neutral": "➖", "irrelevant": "·"}
RESPONSE_LABEL = {"accept": "✅ Accepted", "rebut": "💬 Rebutted", "dismiss": "🙈 Dismissed"}
DECISION_LABEL = {"still_keen": "🐂 Still keen", "less_sure": "🤔 Less sure", "dropping": "🐻 Dropping it"}
CHANGE_LABEL = {"stronger": "🐂 stronger", "weaker": "🐻 weaker", "unchanged": "➖ unchanged",
                "no_news": "❔ no new news"}
STATUS_LABEL = {"supported": "✅ supported", "contradicted": "❌ contradicted", "mixed": "⚖️ mixed",
                "no_evidence": "❔ no evidence found"}
NUMBER_LABEL = {"verified": " <i>(numbers checked ✓)</i>", "mismatch": " ⚠️ <i>number doesn't match the cited fact</i>"}


def uid(update):
    return f"tg:{update.effective_user.id}"


def esc(text):
    return html.escape(str(text or ""))


def buttons(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in rows])


def risk_buttons(prefix):
    return buttons([[("Low", f"{prefix}:low"), ("Medium", f"{prefix}:medium"), ("High", f"{prefix}:high")]])


def cite(ids, sources):
    '''[S1] citations as links to the article; [F1] financial facts as plain text.'''
    urls = {s["id"]: s["url"] for s in sources}
    return " ".join(f'<a href="{esc(urls[i])}">[{i}]</a>' if i in urls else f"[{esc(i)}]"
                    for i in ids if i in urls or str(i).startswith("F"))


def snapshot_text(ticker, facts):
    by_label = {f["label"]: f for f in facts}
    shown = [by_label[label] for label in ["Share price", "Price change today", *financials.SNAPSHOT]
             if label in by_label]
    if not shown:
        return f"📊 <b>{esc(ticker)}</b>: no financial data available."
    return f"📊 <b>{esc(ticker)}</b>\n" + "\n".join(f"{esc(f['label'])}: <b>{esc(financials.format_value(f))}</b>"
                                                     for f in shown)


def fit(line, limit=MAX_MESSAGE):
    '''A line that fits in one message. Cutting HTML mid-tag or mid-entity makes Telegram reject the
    message, so an over-long line falls back to plain text.'''
    if len(line) <= limit:
        return line
    return re.sub(r"&[#\w]*$", "", re.sub(r"<[^>]*>", "", line)[:limit])


def fold(title, lines):
    '''A collapsed section: the title stays visible, the lines expand on tap. Returned as blocks that
    each fit in a message, so a long section becomes several quotes rather than broken HTML.'''
    if not lines:
        return []
    room = MAX_MESSAGE - len(title) - 60
    quotes, body = [], []
    for line in lines:
        line = fit(line, room)
        if body and len("\n".join(body)) + len(line) + 1 > room:
            quotes.append(body)
            body = []
        body.append(line)
    quotes.append(body)
    blocks = ["<blockquote expandable>" + "\n".join(q) + "</blockquote>" for q in quotes]
    blocks[0] = title + "\n" + blocks[0]
    return blocks


async def send_blocks(message, blocks, **kwargs):
    '''Send blocks (lines or folded sections) packed into as few messages as fit, splitting only between
    blocks so no tag is cut. kwargs (e.g. buttons) go on the last message.'''
    lines = []
    for block in blocks:
        # fold() output already fits; an over-long block falls back to plain text, split into lines that fit
        if "<blockquote" in block or len(block) <= MAX_MESSAGE:
            lines.append(block)
        else:
            plain = re.sub(r"<[^>]*>", "", block)
            lines += [fit(line) for line in plain.split("\n")]
    chunk = ""
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > MAX_MESSAGE:
            if chunk.strip():   # Telegram rejects a message that is only blank lines
                await message.reply_text(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            chunk = ""
        if chunk or line.strip():
            chunk += line + "\n"
    return await message.reply_text(chunk if chunk.strip() else "…", parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True, **kwargs)


async def send_long(message, text, **kwargs):
    '''Split on line breaks so no message goes over Telegram's limit.'''
    return await send_blocks(message, text.split("\n"), **kwargs)


# ---------- Start / help ----------

HELP = ("Send me a stock (e.g. <code>AAPL</code> or <code>apple</code>) and I'll gather the latest news, "
        "summarise it with sources, then argue against your reason for liking it.\n\n"
        "/analyse – start a new analysis\n"
        "/checkin – revisit your past theses against the news since (or /checkin AAPL)\n"
        "/memory – what I remember about you (turn memory on/off, delete single entries)\n"
        "/blindspots – kinds of points you tend to dismiss\n"
        "/risk – set your general risk appetite\n"
        "/forget – delete everything I've stored about you\n"
        "/cancel – stop the current question")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = user.upsert_user(uid(update), name=update.effective_user.first_name)
    await update.message.reply_text(
        f"🐂 Bullish by instinct. 🐻 Bear by design.\n\n{HELP}\n\n"
        "If you let me, I remember your reasons, risk appetite and how you respond to my objections "
        "so I can point out what you tend to brush off and link your ideas across similar stocks. "
        "/memory shows it all and lets you delete any of it.\n\n" + DISCLAIMER,
        parse_mode=ParseMode.HTML)
    await update.message.reply_text("Can I remember your past analyses?", reply_markup=memory_buttons())
    if not profile.get("generalRisk"):
        await update.message.reply_text("First, what's your general risk appetite?",
                                        reply_markup=risk_buttons("grisk"))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def risk_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = user.get_user(uid(update)) or {}
    current = profile.get("generalRisk") or "not set"
    await update.message.reply_text(f"Your general risk appetite is <b>{esc(current)}</b>. Change it?",
                                    parse_mode=ParseMode.HTML, reply_markup=risk_buttons("grisk"))


async def general_risk_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    level = query.data.split(":")[1]
    user.upsert_user(uid(update), general_risk=level)
    await query.edit_message_text(f"General risk appetite set to <b>{level}</b>. "
                                  "Send me a stock to get started.", parse_mode=ParseMode.HTML)


# ---------- Analysis conversation ----------

async def analyse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # An explicit /analyse means they're done rebutting
    context.user_data.pop("awaiting_rebuttal", None)
    if context.args:
        return await ask_intent(update, context, " ".join(context.args))
    await update.message.reply_text("Which stock? Send a ticker or company name.")
    return ConversationHandler.END


async def text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    '''Free text is either a rebuttal (after pressing Rebut) or a stock to analyse.'''
    if context.user_data.get("awaiting_rebuttal"):
        await handle_rebuttal(update, context)
        # None keeps whatever step of a new analysis they were on
        return None
    return await ask_intent(update, context, update.message.text.strip())


async def ask_intent(update, context, stock):
    try:
        info = await asyncio.to_thread(agent.lookup_stock, stock)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {esc(e)}. Try a ticker like <code>AAPL</code>.",
                                        parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    except Exception:
        # Finnhub hiccup: carry on without the snapshot, the run looks it up again
        log.exception("Stock lookup failed")
        info = None
    context.user_data["analysis"] = {"stock": stock, "ticker": info["ticker"] if info else None}
    if info:
        await update.message.reply_text(snapshot_text(info["ticker"], info["facts"]), parse_mode=ParseMode.HTML)
    await update.message.reply_text(
        f"Looking at <b>{esc(info['ticker'] if info else stock)}</b>. "
        "Are you just curious, or looking for a reason to buy or sell?",
        parse_mode=ParseMode.HTML,
        reply_markup=buttons([[("🔍 Just curious", "intent:curious"), ("📈 Buy/sell signal", "intent:signal")]]))
    return INTENT


async def intent_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    intent = query.data.split(":")[1]
    context.user_data.setdefault("analysis", {})["intent"] = intent
    stock = context.user_data["analysis"].get("stock", "this stock")
    label = "just curious" if intent == "curious" else "looking for a signal"
    await query.edit_message_text(f"<b>{esc(stock)}</b>: {label}.", parse_mode=ParseMode.HTML)
    await query.message.reply_text(
        f"Why are you interested in {esc(stock)}? Tell me your reason in a sentence or two "
        "(or /skip).", parse_mode=ParseMode.HTML)
    return REASON


async def reason_given(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_rebuttal"):
        # They pressed Rebut on an older point mid-flow; answer that, then keep waiting for the reason
        await handle_rebuttal(update, context)
        return REASON
    analysis = context.user_data.setdefault("analysis", {})
    analysis["reason"] = update.message.text.strip()
    thinking = await update.message.reply_text("🧪 Working out what your reason assumes…")
    try:
        assumptions = await asyncio.to_thread(agent.extract_assumptions,
                                              analysis.get("ticker") or analysis.get("stock"),
                                              analysis["reason"], analysis.get("intent"), uid(update))
    except Exception:
        log.exception("Assumption extraction failed")
        assumptions = None
    if not assumptions:
        # None: the run tries again; nothing to confirm here
        analysis["assumptions"] = None
        await thinking.edit_text("Got it.")
        return await ask_stock_risk(update, context)
    analysis["assumptions"] = assumptions
    await thinking.edit_text(assumptions_text(assumptions), parse_mode=ParseMode.HTML,
                             reply_markup=buttons([[("✅ Check these", "asm:ok"), ("✏️ Rewrite", "asm:edit")]]))
    return ASSUMPTIONS


def assumptions_text(assumptions):
    lines = ["🧪 <b>Your reason assumes:</b>"] + [f"{esc(a['id'])}. {esc(a['text'])}" for a in assumptions]
    lines.append("\nI'll search the news for each and the critic will say if the evidence backs it.")
    return "\n".join(lines)


async def assumptions_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    analysis = context.user_data.setdefault("analysis", {})
    if query.data == "asm:edit":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Send your assumptions, one per line (up to {agent.MAX_ASSUMPTIONS}).",
                                       reply_markup=ForceReply(selective=True))
        return EDIT_ASSUMPTIONS
    await query.edit_message_reply_markup(reply_markup=None)
    return await ask_stock_risk(update, context)


async def assumptions_edited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_rebuttal"):
        await handle_rebuttal(update, context)
        return EDIT_ASSUMPTIONS
    analysis = context.user_data.setdefault("analysis", {})
    lines = [line.strip(" -•*\t") for line in update.message.text.splitlines()]
    assumptions = agent.make_assumptions(analysis.get("ticker") or analysis.get("stock"), [l for l in lines if l])
    if not assumptions:
        await update.message.reply_text("I didn't catch any. Send one assumption per line.")
        return EDIT_ASSUMPTIONS
    analysis["assumptions"] = assumptions
    await update.message.reply_text(assumptions_text(assumptions), parse_mode=ParseMode.HTML)
    return await ask_stock_risk(update, context)


async def assumptions_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    '''/skip at the assumption step keeps the ones shown.'''
    return await ask_stock_risk(update, context)


async def reason_skipped(update: Update, context: ContextTypes.DEFAULT_TYPE):
    analysis = context.user_data.setdefault("analysis", {})
    analysis["reason"], analysis["assumptions"] = None, []
    return await ask_stock_risk(update, context)


async def ask_stock_risk(update, context):
    stock = context.user_data["analysis"].get("ticker") or context.user_data["analysis"].get("stock", "this stock")
    message = update.message or update.callback_query.message
    await message.reply_text(f"How much risk are you willing to take on {esc(stock)}?",
                                    parse_mode=ParseMode.HTML, reply_markup=risk_buttons("srisk"))
    return STOCK_RISK


async def stock_risk_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    request = context.user_data.pop("analysis", {})
    request["stock_risk"] = query.data.split(":")[1]
    await query.edit_message_text(f"Risk appetite for this stock: <b>{request['stock_risk']}</b>.",
                                  parse_mode=ParseMode.HTML)
    status = await query.message.reply_text(
        f"🐻 Researching <b>{esc(request.get('stock'))}</b>: reading the news, then arguing with it. "
        "This takes a minute or two…", parse_mode=ParseMode.HTML)
    # Run in the background so the bot keeps answering other messages
    context.application.create_task(run_and_report(query.message, status, uid(update), request))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled. Send a stock whenever you're ready.")
    return ConversationHandler.END


# ---------- Running the agents and showing results ----------

async def run_and_report(message, status, user_id, request):
    try:
        out = await asyncio.to_thread(agent.run_analysis, user_id, request["stock"],
                                      reason=request.get("reason"), stock_risk=request.get("stock_risk"),
                                      intent=request.get("intent"), n=NEWS_COUNT,
                                      assumptions=request.get("assumptions"), ticker=request.get("ticker"))
    except ValueError as e:
        await status.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return
    except Exception as e:
        log.exception("Analysis failed")
        await status.edit_text(f"⚠️ Something went wrong while researching: {esc(type(e).__name__)}. "
                               "Please try again in a minute.", parse_mode=ParseMode.HTML)
        return

    await status.edit_text(f"✅ Done researching <b>{esc(out['ticker'])}</b>.", parse_mode=ParseMode.HTML)
    # One failed message (e.g. Telegram rejecting it) shouldn't lose the rest of the results
    for send in (send_report, send_critique, send_objections):
        try:
            await send(message, out)
        except Exception:
            log.exception("Sending %s failed", send.__name__)
    await message.reply_text("After hearing the case against, where do you stand?",
                             reply_markup=buttons([[(label, f"dec:{out['sessionId']}:{key}")
                                                    for key, label in DECISION_LABEL.items()]]))


def source_line(s):
    partial = {"teaser": " (preview)", "summary": " (summary only)"}.get(s.get("textSource"), "")
    found = "🔎 " if s.get("origin") == "search" else ""
    return (f'{s["id"]} {found}{SENTIMENT_EMOJI.get(s.get("sentiment"), "")} {esc(s.get("date"))} · '
            f'{esc(s.get("source"))} · <a href="{esc(s["url"])}">{esc(str(s["headline"])[:HEADLINE_CHARS])}</a>{partial}')


async def send_report(message, out):
    '''The gist stays visible; facts, sources and extra key points fold away.'''
    sources, summary, counts = out["sources"], out["summary"], out["counts"]
    mood = " ".join(f"{SENTIMENT_EMOJI[k]} {k} {v}" for k, v in counts.items() if v)
    full = sum(s.get("textSource") == "article" for s in sources)
    blocks = [f"📰 <b>{esc(out['ticker'])}: what the news says</b>",
              f"Overall: <b>{esc(summary.get('overall_sentiment', 'mixed'))}</b> · {mood}",
              f"<i>{full} of {len(sources)} sources read in full</i>", "",
              esc(summary.get("summary", ""))]
    points = []
    for kp in summary.get("key_points") or []:
        flag = " ⚠️ <i>unverified</i>" if kp.get("unverified") else NUMBER_LABEL.get(kp.get("numberCheck"), "")
        points.append(f"• {esc(kp.get('point'))} {cite(kp.get('sources', []), sources)}{flag}")
    if points:
        blocks += ["", "<b>Key points</b>", *points[:KEY_POINTS_SHOWN]]
        blocks += fold(f"<i>+{len(points) - KEY_POINTS_SHOWN} more</i>", points[KEY_POINTS_SHOWN:])
    blocks.append("")
    blocks += fold("📊 <b>Financial facts</b> (Finnhub)",
                   [f'{esc(f["id"])} {esc(f["label"])} ({esc(f["period"])}): {esc(financials.format_value(f))}'
                    for f in out.get("facts") or []])
    searched = sum(s.get("origin") == "search" for s in sources)
    blocks += fold(f"📚 <b>{len(sources)} sources</b>" + (f" ({searched} 🔎 found for your claims)" if searched else ""),
                   [source_line(s) for s in sources])
    if out.get("remembered") is False:
        blocks += ["", "<i>Memory is off: this analysis won't be used in future runs.</i>"]
    blocks += ["", DISCLAIMER]
    await send_blocks(message, blocks)


async def send_critique(message, out):
    '''Assumption verdicts and the critic's verdict stay visible; extra issues fold away.'''
    critique = out["critique"]
    blocks = []
    if out.get("assumptions"):
        blocks.append("🧪 <b>Your assumptions vs the evidence</b>")
        for a in out["assumptions"]:
            blocks.append(f"{a['id']}. {esc(a['text'])}: <b>{STATUS_LABEL.get(a.get('status'), '❔')}</b>")
            if a.get("note"):
                blocks.append(f"   <i>{esc(a['note'])}</i> {cite(a.get('sources', []), out['sources'])}"
                              + NUMBER_LABEL.get(a.get("numberCheck"), ""))
        blocks.append("")
    blocks.append("🔍 <b>Critic: holes in the reasoning</b>")
    if critique.get("verdict"):
        blocks.append(f"<b>Verdict:</b> {esc(critique['verdict'])}")
    issues = []
    for issue in critique.get("issues") or []:
        who = "your reason" if issue.get("target") == "user" else "the summary"
        issues.append(f"• <i>{who}:</i> {esc(issue.get('issue'))} {cite(issue.get('sources', []), out['sources'])}"
                      + NUMBER_LABEL.get(issue.get("numberCheck"), ""))
    blocks += issues[:ISSUES_SHOWN] or ["No problems found."]
    blocks += fold(f"<i>+{len(issues) - ISSUES_SHOWN} more</i>", issues[ISSUES_SHOWN:])
    await send_blocks(message, blocks)


def objection_text(o, sources, header=True):
    lines = []
    if header:
        lines.append(f"😈 <b>Devil's advocate · {esc(o['category'])}</b>")
        if o.get("whyFirst"):
            lines.append(f"<i>Shown first: {esc(o['whyFirst'])}</i>")
    lines.append(f"{esc(o['claim'])} {cite(o.get('sources', []), sources)}")
    if o.get("unverified"):
        lines.append("⚠️ <i>No valid source cited</i>")
    elif o.get("numberCheck") == "mismatch":
        lines.append("⚠️ <i>A number here doesn't match the cited financial fact</i>")
    return "\n".join(lines)


def objection_buttons(objection_id, rebut_label="💬 Rebut"):
    return buttons([[("✅ Fair point", f"obj:{objection_id}:accept"),
                     (rebut_label, f"obj:{objection_id}:rebut"),
                     ("🙈 Dismiss", f"obj:{objection_id}:dismiss")]])


async def send_objections(message, out):
    if not out["objections"]:
        await message.reply_text("😈 The devil's advocate couldn't build a case from these sources.")
        return
    await message.reply_text(f"😈 <b>The case against {esc(out['ticker'])}</b>: "
                             "respond to each point so I learn what you brush off.",
                             parse_mode=ParseMode.HTML)
    for o in out["objections"]:
        await message.reply_text(objection_text(o, out["sources"]), parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True, reply_markup=objection_buttons(o["_id"]))


# ---------- Responses ----------

async def objection_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, objection_id, response = query.data.split(":")
    user_id = uid(update)
    try:
        objection = user.get_objection(user_id, objection_id)
    except ValueError:
        await query.answer("That point no longer exists.", show_alert=True)
        return

    if response == "rebut":
        await query.answer()
        context.user_data["awaiting_rebuttal"] = objection_id
        await query.message.reply_text(f"What's your counter-argument on <b>{esc(objection['category'])}</b>? "
                                       "(or /cancel)",
                                       parse_mode=ParseMode.HTML, reply_markup=ForceReply(selective=True))
        return

    user.record_response(user_id, objection_id, response)
    await query.answer(RESPONSE_LABEL[response])
    await query.edit_message_reply_markup(reply_markup=buttons([[(RESPONSE_LABEL[response], "noop")]]))


async def handle_rebuttal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    objection_id = context.user_data.pop("awaiting_rebuttal")
    rebuttal = update.message.text.strip()
    thinking = await update.message.reply_text("😈 Searching the news for your claim and considering it…")
    try:
        result = await asyncio.to_thread(agent.rebuttal_reply, uid(update), objection_id, rebuttal)
    except Exception as e:
        log.exception("Rebuttal failed")
        await thinking.edit_text(f"⚠️ Couldn't answer that: {esc(type(e).__name__)}. Try rebutting again.",
                                 parse_mode=ParseMode.HTML)
        return
    session = db.sessions.find_one({"objectionIds": user.to_object_id(objection_id)}) or {}
    verdict = "🏳️ Fair, I concede." if result["verdict"] == "concede" else "😈 I'm holding my ground."
    found = result.get("newSources") or []
    searched = ("\n\n🔎 <i>Searched your claim, found:</i>\n" + "\n".join(
        f'{s["id"]} {SENTIMENT_EMOJI.get(s["sentiment"], "")} <a href="{esc(s["url"])}">{esc(str(s["headline"])[:HEADLINE_CHARS])}</a>'
        for s in found)) if found else "\n\n🔎 <i>Searched your claim: nothing new found.</i>"
    await thinking.edit_text(f"<b>{verdict}</b>\n{esc(result['reply'])} "
                             f"{cite(result.get('sources', []), session.get('sources', []))}{searched}",
                             parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                             reply_markup=objection_buttons(objection_id, "💬 Rebut again"))


async def decision_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, session_id, decision = query.data.split(":")
    try:
        user.record_decision(uid(update), session_id, decision)
    except ValueError:
        await query.answer("Couldn't save that.", show_alert=True)
        return
    await query.answer("Saved")
    await query.edit_message_text(f"Noted: <b>{DECISION_LABEL[decision]}</b>. I'll remember this next time "
                                  "you look at it. /blindspots shows what you tend to dismiss.",
                                  parse_mode=ParseMode.HTML)


async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def expired_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    '''Buttons from a question that's no longer active (e.g. an old intent or risk question).'''
    await update.callback_query.answer("This question has expired. Send a stock to start again.",
                                       show_alert=True)


# ---------- Check-in on past theses ----------

async def checkin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = " ".join(context.args) if context.args else None
    status = await update.message.reply_text("🐻 Checking the news since your past theses…")
    context.application.create_task(run_checkin(update.message, status, uid(update), stock))


async def run_checkin(message, status, user_id, stock):
    try:
        checkins = await asyncio.to_thread(agent.thesis_checkin, user_id, stock)
    except ValueError as e:
        await status.edit_text(f"⚠️ {esc(e)}", parse_mode=ParseMode.HTML)
        return
    except Exception as e:
        log.exception("Check-in failed")
        await status.edit_text(f"⚠️ Something went wrong during the check-in: {esc(type(e).__name__)}.",
                               parse_mode=ParseMode.HTML)
        return
    await status.edit_text(f"🐻 Checked {len(checkins)} past {'thesis' if len(checkins) == 1 else 'theses'}.")
    for c in checkins:
        try:
            await send_checkin(message, c)
        except Exception:
            log.exception("Sending check-in failed")


def price_line(c):
    then, now = c.get("priceThen"), c.get("priceNow")
    if not then or not now:
        return None
    return f"Price: ${then:,.2f} → <b>${now:,.2f}</b> ({(now / then - 1) * 100:+.1f}%)"


async def send_checkin(message, c):
    decided = f", you decided: {DECISION_LABEL[c['decision']]}" if c.get("decision") in DECISION_LABEL else ""
    blocks = [f"🐻 <b>Check-in: {esc(c['ticker'])}</b>",
              f"<i>On {esc(c['since'])} you said: \"{esc(c.get('reason') or 'no reason given')}\"{decided}</i>"]
    if price_line(c):
        blocks.append(price_line(c))
    blocks.append("")
    for a in c["assumptions"]:
        blocks.append(f"{a['id']}. {esc(a['text'])}: <b>{CHANGE_LABEL.get(a.get('change'), '❔')}</b>")
        if a.get("note"):
            blocks.append(f"   <i>{esc(a['note'])}</i> {cite(a.get('sources', []), c['sources'])}"
                          + NUMBER_LABEL.get(a.get("numberCheck"), ""))
    blocks += ["", esc(c["summary"]), ""]
    blocks += fold(f"📚 <b>{len(c['sources'])} new sources</b>", [source_line(s) for s in c["sources"]])
    await send_blocks(message, blocks)
    # Buttons on their own message, so answering them doesn't overwrite the check-in
    await message.reply_text(f"Where do you stand now on {esc(c['ticker'])}?", parse_mode=ParseMode.HTML,
                             reply_markup=buttons([[(label, f"dec:{c['sessionId']}:{key}")
                                                    for key, label in DECISION_LABEL.items()]]))


# ---------- Memory and blind spots ----------

async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory = user.get_memory(uid(update))
    profile = memory["user"] or {}
    on = memory["memoryOn"]
    lines = ["🧠 <b>What I remember about you</b>",
             f"Memory: <b>{'on' if on else 'off'}</b>"
             + ("" if on else " (new analyses aren't remembered)"),
             f"General risk appetite: <b>{esc(profile.get('generalRisk') or 'not set')}</b>", ""]
    rows = []
    if memory["theses"]:
        lines.append("<b>Your recent theses</b>")
        for n, t in enumerate(memory["theses"], 1):
            decided = f" → {DECISION_LABEL[t['decision']]}" if t.get("decision") else ""
            lines.append(f"{n}. {t['createdAt'].date().isoformat()} <b>{esc(t.get('ticker') or t['stock'])}</b>: "
                         f"{esc(t.get('reason') or 'no reason given')} "
                         f"(risk {esc(t.get('stockRisk') or '?')}){decided}"
                         + (" <i>(memory was off: not used)</i>" if t.get("remembered") is False else ""))
            for a in t.get("assumptions") or []:
                lines.append(f"   {esc(a.get('text'))}: {STATUS_LABEL.get(a.get('status'), '❔')}")
            rows.append((f"🗑 {n}", f"delth:{t['_id']}"))
    else:
        lines.append("No theses yet.")
    spots = [f"{esc(c)} {v['dismissed']}/{v['total']}" + (" (muted)" if v["muted"] else "")
             for c, v in memory["blindSpots"].items()]
    lines += ["", "<b>Dismissed by category:</b> " + (", ".join(spots) if spots else "nothing yet")]
    if rows:
        lines += ["", "Tap 🗑 n to forget one thesis and your responses to it."]
    keyboard = [rows[i:i + 5] for i in range(0, len(rows), 5)]
    keyboard.append([("⏸ Turn memory off", "mem:off") if on else ("▶️ Turn memory on", "mem:on"),
                     ("🗑 Forget everything", "forget:ask")])
    await send_long(update.message, "\n".join(lines), reply_markup=buttons(keyboard))


def memory_buttons():
    return buttons([[("✅ Yes, remember", "mem:on"), ("🚫 Don't remember", "mem:off")]])


async def memory_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    on = query.data == "mem:on"
    user.set_memory(uid(update), on)
    await query.answer("Memory on" if on else "Memory off")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "🧠 Memory is <b>on</b>: I'll use your past analyses to challenge you. /memory to review or delete."
        if on else "🧠 Memory is <b>off</b>: new analyses won't be remembered or used later. "
                   "Turn it back on in /memory.", parse_mode=ParseMode.HTML)


async def delete_thesis_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        thesis = user.delete_thesis(uid(update), query.data.split(":", 1)[1])
    except ValueError:
        await query.answer("Already deleted.", show_alert=True)
        return
    await query.answer("Deleted")
    await query.message.reply_text(f"🗑 Forgot your {esc(thesis.get('ticker') or thesis['stock'])} thesis from "
                                   f"{thesis['createdAt'].date().isoformat()} and your responses to it. "
                                   "/memory to see what's left.", parse_mode=ParseMode.HTML)


async def blindspots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spots = user.get_blind_spots(uid(update), include_muted=True)
    if not spots:
        await update.message.reply_text("No blind spots yet. Respond to a few objections first.")
        return
    lines = ["🙈 <b>Your blind spots</b>",
             "Points you dismiss most come first in future objections. Mute a category if you "
             "ignore it on purpose.", ""]
    rows = []
    for c, v in spots.items():
        bar = "█" * v["dismissed"] + "░" * (v["total"] - v["dismissed"])
        lines.append(f"<b>{esc(c)}</b> {bar} {v['dismissed']}/{v['total']} dismissed"
                     + (" · muted" if v["muted"] else ""))
        rows.append([(f"🔊 Unmute {c}", f"unmute:{c}") if v["muted"] else (f"🔇 Mute {c}", f"mute:{c}")])
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=buttons(rows))


async def mute_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, category = query.data.split(":", 1)
    user.mute_category(uid(update), category, mute=action == "mute")
    await query.answer(f"{category} {'muted' if action == 'mute' else 'unmuted'}")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"{'🔇 Muted' if action == 'mute' else '🔊 Unmuted'} <b>{esc(category)}</b>. "
                                   "/blindspots to see the profile.", parse_mode=ParseMode.HTML)


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Delete everything I've stored about you? This can't be undone.",
                                    reply_markup=buttons([[("🗑 Yes, forget me", "forget:yes"),
                                                           ("Keep it", "forget:no")]]))


async def forget_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    if choice == "ask":
        await query.message.reply_text("Delete everything I've stored about you? This can't be undone.",
                                       reply_markup=buttons([[("🗑 Yes, forget me", "forget:yes"),
                                                              ("Keep it", "forget:no")]]))
    elif choice == "yes":
        counts = user.forget_user(uid(update))
        context.user_data.clear()
        await query.edit_message_text(f"Done. Deleted {sum(counts.values())} records. /start to begin again.")
    else:
        await query.edit_message_text("Kept everything.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


def build_app(token):
    # Updates run concurrently so one user's slow rebuttal or lookup doesn't hold up everyone else
    app = Application.builder().token(token).concurrent_updates(True).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler("analyse", analyse_cmd), CommandHandler("analyze", analyse_cmd),
                      MessageHandler(filters.TEXT & ~filters.COMMAND, text_entry)],
        states={
            INTENT: [CallbackQueryHandler(intent_chosen, pattern=r"^intent:")],
            REASON: [CommandHandler("skip", reason_skipped),
                     MessageHandler(filters.TEXT & ~filters.COMMAND, reason_given)],
            # Typing instead of pressing ✏️ Rewrite also rewrites; a new stock needs /analyse here
            ASSUMPTIONS: [CallbackQueryHandler(assumptions_chosen, pattern=r"^asm:"),
                          CommandHandler("skip", assumptions_skipped),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, assumptions_edited)],
            EDIT_ASSUMPTIONS: [CommandHandler("skip", assumptions_skipped),
                               MessageHandler(filters.TEXT & ~filters.COMMAND, assumptions_edited)],
            STOCK_RISK: [CallbackQueryHandler(stock_risk_chosen, pattern=r"^srisk:")],
        },
        # Restarting lives in fallbacks, not allow_reentry: fallbacks are only tried after the
        # current step's handlers, so a typed reason isn't mistaken for a new stock
        fallbacks=[CommandHandler("cancel", cancel),
                   CommandHandler("analyse", analyse_cmd), CommandHandler("analyze", analyse_cmd),
                   MessageHandler(filters.TEXT & ~filters.COMMAND, text_entry)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("risk", risk_cmd))
    app.add_handler(CommandHandler("checkin", checkin_cmd))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("blindspots", blindspots_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(conversation)
    app.add_handler(CommandHandler("cancel", cancel))  # outside a conversation, e.g. to stop a rebuttal
    app.add_handler(CallbackQueryHandler(general_risk_chosen, pattern=r"^grisk:"))
    app.add_handler(CallbackQueryHandler(objection_response, pattern=r"^obj:"))
    app.add_handler(CallbackQueryHandler(decision_chosen, pattern=r"^dec:"))
    app.add_handler(CallbackQueryHandler(mute_chosen, pattern=r"^(mute|unmute):"))
    app.add_handler(CallbackQueryHandler(forget_chosen, pattern=r"^forget:"))
    app.add_handler(CallbackQueryHandler(memory_chosen, pattern=r"^mem:(on|off)$"))
    app.add_handler(CallbackQueryHandler(delete_thesis_chosen, pattern=r"^delth:"))
    app.add_handler(CallbackQueryHandler(noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(expired_button))  # last: anything nothing else handled
    app.add_error_handler(on_error)
    return app


if __name__ == "__main__":
    if not CONSTANTS.TELEGRAM_API_KEY:
        sys.exit("TELEGRAM_API_KEY is missing from .env")
    build_app(CONSTANTS.TELEGRAM_API_KEY).run_polling(allowed_updates=Update.ALL_TYPES)
