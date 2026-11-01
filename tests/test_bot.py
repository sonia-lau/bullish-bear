'''Telegram bot through PTB's real handler dispatch (app.process_update), with Telegram's network faked.
This catches routing bugs (which handler gets a message) that calling handlers directly can't.'''

import asyncio
import itertools
import re
import time
from html.parser import HTMLParser

import pytest
from telegram import Message, Update, User
from telegram.ext import ExtBot

import bot
import db
import user

NOW = int(time.time())
FROM = {"id": 42, "is_bot": False, "first_name": "Sam"}
CHAT = {"id": 42, "type": "private"}
BOT_USER = {"id": 999, "is_bot": True, "first_name": "Bear"}


class TagChecker(HTMLParser):
    '''Telegram rejects a whole message if its HTML tags don't balance.'''
    def __init__(self):
        super().__init__()
        self.open = []

    def handle_starttag(self, tag, attrs):
        self.open.append(tag)

    def handle_endtag(self, tag):
        assert self.open and self.open.pop() == tag, f"unbalanced </{tag}>"


BAD_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


def check_html(text):
    assert not BAD_ENTITY.search(text), f"broken &entity in {text[-80:]!r}"
    checker = TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.open, f"unclosed tags {checker.open} in {text[:200]!r}"


class FakeTelegram:
    def __init__(self, monkeypatch):
        self.out = []          # (kind, text, reply_markup)
        self.ids = itertools.count(1000)
        self.tasks = []
        tg = self

        def message(b, chat_id, text):
            return Message.de_json({"message_id": next(tg.ids), "date": NOW, "chat": {"id": chat_id, "type": "private"},
                                    "from": BOT_USER, "text": text}, b)

        async def send_message(self, chat_id, text, *a, reply_markup=None, **k):
            assert len(text) <= 4096, "message over Telegram's limit"
            if k.get("parse_mode") == "HTML":
                check_html(text)
            tg.out.append(("send", text, reply_markup))
            return message(self, chat_id, text)

        async def edit_message_text(self, text, chat_id=None, message_id=None, *a, reply_markup=None, **k):
            if k.get("parse_mode") == "HTML":
                check_html(text)
            tg.out.append(("edit", text, reply_markup))
            return message(self, chat_id or 42, text)

        async def edit_message_reply_markup(self, *a, reply_markup=None, **k):
            tg.out.append(("markup", None, reply_markup))
            return True

        async def answer_callback_query(self, callback_query_id, text=None, *a, **k):
            tg.out.append(("answer", text, None))
            return True

        for name, fn in [("send_message", send_message), ("edit_message_text", edit_message_text),
                         ("edit_message_reply_markup", edit_message_reply_markup),
                         ("answer_callback_query", answer_callback_query)]:
            monkeypatch.setattr(ExtBot, name, fn)

        self.app = bot.build_app("123456:TEST")
        # bot.on_error only logs; collect errors so a crashing handler fails the test
        self.errors = []

        async def collect(update, context):
            self.errors.append(context.error)
        self.app.add_error_handler(collect)
        self.app.bot._bot_user = User(999, "Bear", True, username="bullish_bear_bot")
        self.app.bot._initialized = True
        self.app._initialized = True
        # Background analyses are collected so each step can wait for them
        self.app.create_task = lambda coro, *a, **k: self.tasks.append(asyncio.ensure_future(coro)) or self.tasks[-1]
        self.loop = asyncio.new_event_loop()

    def _update(self, payload):
        return Update.de_json({"update_id": next(self.ids), **payload}, self.app.bot)

    def send(self, update):
        async def go():
            await self.app.process_update(update)
            while self.tasks:
                await asyncio.gather(*self.tasks)
                self.tasks.clear()
        self.loop.run_until_complete(go())

    def text(self, text):
        msg = {"message_id": next(self.ids), "date": NOW, "chat": CHAT, "from": FROM, "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        self.send(self._update({"message": msg}))

    def press(self, data):
        self.send(self._update({"callback_query": {
            "id": str(next(self.ids)), "from": FROM, "chat_instance": "c", "data": data,
            "message": {"message_id": 5, "date": NOW, "chat": CHAT, "from": BOT_USER, "text": "q"}}}))

    def last(self):
        return [t for _, t, _ in self.out if t][-1]

    def texts(self):
        return "\n".join(t for _, t, _ in self.out if t)

    def buttons(self, prefix=""):
        return [b.callback_data for _, _, m in self.out if m and hasattr(m, "inline_keyboard")
                for row in m.inline_keyboard for b in row if b.callback_data.startswith(prefix)]

    def close(self):
        self.loop.close()


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram(monkeypatch)
    yield fake
    fake.close()
    assert fake.errors == [], f"handler crashed: {fake.errors!r}"


def full_run(tg, stock="AAPL", reason="negative press on iPhone sales", confirm=True):
    tg.text(stock)
    tg.press("intent:signal")
    tg.text(reason)
    if confirm:
        tg.press("asm:ok")
    tg.press("srisk:high")


# ---------- Start and memory consent ----------

def test_start_asks_memory_and_risk(tg):
    tg.text("/start")
    assert "mem:on" in tg.buttons() and "grisk:low" in tg.buttons()
    tg.press("mem:off")
    assert user.memory_on("tg:42") is False and "Memory is <b>off</b>" in tg.last()
    tg.press("grisk:medium")
    assert user.get_user("tg:42")["generalRisk"] == "medium"


# ---------- Analysis flow ----------

def test_stock_shows_snapshot_before_intent(tg):
    tg.text("apple")
    texts = [t for _, t, _ in tg.out]
    assert "📊 <b>AAPL</b>" in texts[0] and "P/E ratio" in texts[0] and "$230.50" in texts[0]
    assert "Looking at <b>AAPL</b>" in texts[1]


def test_unknown_company_ends_flow(tg):
    tg.text("zzzz")
    assert "No company found" in tg.last()
    tg.press("intent:signal")
    assert "expired" in tg.out[-1][1]


def test_lookup_outage_still_lets_user_continue(tg, monkeypatch):
    import agent
    monkeypatch.setattr(agent, "lookup_stock", lambda s: (_ for _ in ()).throw(RuntimeError("finnhub down")))
    tg.text("apple")
    assert "Looking at <b>apple</b>" in tg.last()


def test_full_flow_shows_assumptions_report_and_objections(tg):
    full_run(tg)
    all_text = tg.texts()
    assert "Your reason assumes" in all_text and "iPhone sales are falling" in all_text
    assert "what the news says" in all_text and "Financial facts" in all_text
    assert "🔎" in all_text                                    # web-search source marked
    assert "Your assumptions vs the evidence" in all_text and "✅ supported" in all_text
    assert "(numbers checked ✓)" in all_text
    assert len(tg.buttons("obj:")) == 6 and len(tg.buttons("dec:")) == 3
    thesis = db.theses.find_one({})
    assert thesis["reason"] == "negative press on iPhone sales" and thesis["ticker"] == "AAPL"


def test_rewrite_assumptions(tg, llm):
    tg.text("AAPL")
    tg.press("intent:signal")
    tg.text("iPhone sales")
    tg.press("asm:edit")
    assert "one per line" in tg.last()
    tg.text("- Services revenue growing\n\n• China demand recovering")
    assert "How much risk" in tg.last()
    tg.press("srisk:low")
    assert db.theses.find_one({})["assumptions"][1]["text"] == "China demand recovering"
    assert llm.roles().count("assumptions") == 1             # not extracted again during the run


def test_typing_at_assumption_buttons_rewrites(tg):
    tg.text("AAPL")
    tg.press("intent:signal")
    tg.text("iPhone sales")
    tg.text("Margins are expanding")                          # instead of pressing ✏️ Rewrite
    assert "How much risk" in tg.last()
    tg.press("srisk:low")
    assert db.theses.find_one({})["assumptions"][0]["text"] == "Margins are expanding"


@pytest.mark.parametrize("edit_first", [False, True])
def test_skip_at_assumptions_keeps_them(tg, edit_first):
    tg.text("AAPL")
    tg.press("intent:signal")
    tg.text("iPhone sales")
    if edit_first:
        tg.press("asm:edit")
    tg.text("/skip")
    assert "How much risk" in tg.last()
    tg.press("srisk:low")
    assert db.theses.find_one({})["assumptions"][0]["text"] == "iPhone sales are falling"


def test_empty_rewrite_asks_again(tg):
    tg.text("AAPL")
    tg.press("intent:signal")
    tg.text("iPhone sales")
    tg.press("asm:edit")
    tg.text("-\n•")
    assert "didn't catch any" in tg.last()
    tg.text("Services growing")
    assert "How much risk" in tg.last()


def test_number_mismatch_is_flagged(tg, llm):
    llm.responses["devil"] = {"objections": [{"category": "valuation", "claim": "P/E of 45 is rich.",
                                              "sources": ["S1", "F3"]}]}
    llm.responses["critic"] = {"assumptions": [{"id": "A1", "status": "contradicted", "note": "P/E is 12",
                                                "sources": ["F3"]}], "issues": [], "verdict": "v"}
    full_run(tg)
    all_text = tg.texts()
    assert "A number here doesn't match the cited financial fact" in all_text
    assert "❌ contradicted" in all_text and "number doesn't match the cited fact" in all_text


def test_skip_reason_has_no_assumptions(tg, world):
    tg.text("AAPL")
    tg.press("intent:curious")
    tg.text("/skip")
    assert "How much risk" in tg.last()
    tg.press("srisk:low")
    assert world.counts["search"] == 0 and db.theses.find_one({})["reason"] is None


def test_assumption_extraction_failure_goes_to_risk(tg, llm):
    llm.responses["assumptions"] = lambda p: (_ for _ in ()).throw(RuntimeError("down"))
    tg.text("AAPL")
    tg.press("intent:signal")
    tg.text("iPhone sales")
    assert "How much risk" in tg.last()


def test_new_stock_mid_flow_restarts(tg):
    tg.text("AAPL")
    tg.press("intent:signal")
    tg.text("iPhone sales")
    tg.press("asm:ok")
    tg.text("nvda")                                            # typed at the risk buttons
    assert "Looking at <b>NVDA</b>" in tg.last()
    tg.text("/cancel")
    assert "Cancelled" in tg.last()


def test_stale_buttons_expire(tg):
    tg.press("srisk:low")
    assert tg.out[-1][0] == "answer" and "expired" in tg.out[-1][1]
    tg.press("asm:ok")
    assert "expired" in tg.out[-1][1]


def test_one_failed_message_does_not_lose_the_rest(tg, monkeypatch):
    async def broken(message, out):
        raise RuntimeError("Telegram said no")
    monkeypatch.setattr(bot, "send_critique", broken)
    full_run(tg)
    assert len(tg.buttons("obj:")) == 6 and tg.buttons("dec:")


@pytest.mark.parametrize("cut", ["tag", "entity"])
def test_send_long_never_cuts_html(cut):
    sent = []

    class Msg:
        async def reply_text(self, text, **kwargs):
            check_html(text)
            sent.append(text)

    pad = "x" * (bot.MAX_MESSAGE - (5 if cut == "tag" else 2))
    line = pad + ('<a href="https://e.x">link</a>' if cut == "tag" else "&amp;&amp;")
    asyncio.run(bot.send_long(Msg(), "start\n" + line + "\nend"))
    assert all(len(t) <= 4096 for t in sent) and "end" in sent[-1]


class Recorder:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        check_html(text)
        self.sent.append(text)


def test_send_blocks_splits_long_multiline_block():
    m = Recorder()
    asyncio.run(bot.send_blocks(m, ["title", "<i>" + ("y" * 3000 + "\n") * 3 + "</i>"]))
    assert all(len(t) <= 4096 for t in m.sent) and len(m.sent) >= 2


def test_send_blocks_never_sends_a_blank_message():
    m = Recorder()
    asyncio.run(bot.send_blocks(m, ["a" * 3999, "", "b" * 5000]))
    assert all(t.strip() for t in m.sent) and len(m.sent) == 2


def test_concurrent_updates_enabled(tg):
    assert tg.app.concurrent_updates > 1


def test_failed_run_reports_and_keeps_nothing(tg, llm):
    llm.responses["devil"] = lambda p: (_ for _ in ()).throw(RuntimeError("down"))
    full_run(tg)
    assert "Something went wrong" in tg.last() and db.theses.count_documents({}) == 0


# ---------- Objections and rebuttals ----------

def test_dismiss_then_rebut_with_web_search(tg):
    full_run(tg)
    first, second = tg.buttons("obj:")[0].split(":")[1], tg.buttons("obj:")[3].split(":")[1]
    tg.press(f"obj:{first}:dismiss")
    assert user.get_objection("tg:42", first)["response"] == "dismiss"
    tg.press(f"obj:{second}:rebut")
    assert "counter-argument" in tg.last()
    tg.text("Reuters says demand is strong")
    assert "holding my ground" in tg.last() and "Searched your claim, found" in tg.last()
    assert user.get_objection("tg:42", second)["exchanges"]


def test_rebut_mid_flow_keeps_analysis(tg):
    full_run(tg)
    oid = tg.buttons("obj:")[0].split(":")[1]
    tg.text("TSLA")
    tg.press(f"obj:{oid}:rebut")
    tg.text("Buybacks make it cheap")
    assert "holding my ground" in tg.last()
    tg.press("intent:curious")
    assert "Why are you interested" in tg.last()
    tg.press(f"obj:{oid}:rebut")
    tg.text("Debt is cheap to service")
    tg.text("EV demand recovering")
    assert "Your reason assumes" in tg.last()


def test_cancel_clears_pending_rebuttal(tg):
    full_run(tg)
    oid = tg.buttons("obj:")[0].split(":")[1]
    tg.press(f"obj:{oid}:rebut")
    tg.text("/cancel")
    tg.text("MSFT")
    assert "Looking at <b>MSFT</b>" in tg.last()


def test_checkin_command(tg, world):
    tg.text("/checkin")
    assert "No past analyses" in tg.last()
    full_run(tg)
    world.news = [{"id": 99, "headline": "Fresh AAPL story", "summary": "s", "source": "Reuters",
                   "url": "https://news.example/fresh", "datetime": world.news[0]["datetime"] + 60}] + world.news
    db.news_fetches.delete_many({})
    tg.text("/checkin aapl")
    all_text = tg.texts()
    assert "Check-in: AAPL" in all_text and "🐻 weaker" in all_text and "Where do you stand now on AAPL?" in all_text
    # The buttons sit on their own short message, so answering doesn't overwrite the check-in
    kind, text, markup = tg.out[-1]
    assert markup and text.startswith("Where do you stand now") and "Check-in" not in text
    tg.press(tg.buttons("dec:")[-1])
    assert db.theses.find_one({})["decision"] == "dropping"


def test_checkin_unknown_stock(tg):
    tg.text("/checkin zzzz")
    assert "No company found" in tg.last()


def test_checkin_mid_flow_keeps_analysis(tg):
    full_run(tg)
    tg.text("MSFT")
    tg.text("/checkin")
    assert "Check-in: AAPL" in tg.texts()
    tg.press("intent:curious")
    assert "Why are you interested" in tg.last()


def test_decision_saved(tg):
    full_run(tg)
    tg.press(tg.buttons("dec:")[1])
    assert db.theses.find_one({})["decision"] == "less_sure"


# ---------- Memory commands ----------

def test_memory_shows_theses_and_deletes_one(tg):
    full_run(tg)
    full_run(tg, stock="MSFT", reason="Cloud is booming")
    tg.text("/memory")
    assert "Memory: <b>on</b>" in tg.last() and "Cloud is booming" in tg.last()
    assert "iPhone sales are falling: ✅ supported" in tg.last()
    delete = tg.buttons("delth:")[-2:]
    assert len(delete) == 2
    tg.press(delete[0])                                        # newest first: MSFT
    assert "Forgot your MSFT thesis" in tg.last()
    assert [t["ticker"] for t in user.past_theses("tg:42")] == ["AAPL"]
    tg.press(delete[0])
    assert "Already deleted" in tg.out[-1][1]


def test_memory_toggle(tg):
    tg.text("/memory")
    tg.press("mem:off")
    full_run(tg)
    assert "Memory is off" in tg.texts()
    assert user.past_theses("tg:42") == []
    tg.text("/memory")
    assert "Memory: <b>off</b>" in tg.last() and "mem:on" in tg.buttons()
    # The memory-off run is still listed, so it can be deleted on its own
    assert "memory was off" in tg.last()
    tg.press(tg.buttons("delth:")[-1])
    assert db.theses.count_documents({}) == 0


def test_blindspots_and_mute(tg):
    full_run(tg)
    oid = tg.buttons("obj:")[0].split(":")[1]
    tg.press(f"obj:{oid}:dismiss")
    tg.text("/blindspots")
    assert "valuation" in tg.last()
    tg.press("mute:valuation")
    assert user.get_user("tg:42")["mutedCategories"] == ["valuation"]


def test_forget_everything(tg):
    full_run(tg)
    tg.text("/forget")
    tg.press("forget:yes")
    assert "Deleted" in tg.last() and db.theses.count_documents({}) == 0


def test_long_report_is_split(tg, world, llm):
    world.news[0]["headline"] = "Very long headline & more " * 300
    llm.responses["researcher"] = {"summary": "Long <summary> & " * 400, "overall_sentiment": "mixed",
                                   "key_points": [{"point": "x & y " * 900, "sources": ["S1"]}]}
    full_run(tg)          # FakeTelegram checks every message is under 4096 characters with balanced HTML
    report = [t for k, t, _ in tg.out if k == "send" and t]
    assert any("what the news says" in t for t in report)
    assert "After hearing the case against" in tg.texts()   # the rest still arrived
