#!/usr/bin/env python3
"""Conversation-level tests for the SPIN112 kind (topic) picker.

Drives the real handler functions with lightweight fake ``Update``/``Context``
objects: no Telegram, no network, no tile server, throwaway database.

    python3 tests/test_topic_flow.py
"""
import asyncio
import os
import sys
import tempfile

DB_TMP = os.path.join(tempfile.mkdtemp(prefix="spin_flow_"), "flow.db")
os.environ["SUBSCRIPTIONS_DB"] = DB_TMP

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)

import subscriptions as S  # noqa: E402

S.init(db_path=DB_TMP)

FAILS = []
PASSES = 0


def check(name, cond, detail=""):
    global PASSES
    if cond:
        PASSES += 1
        print("PASS  %s" % name)
    else:
        FAILS.append(name)
        print("FAIL  %s  %s" % (name, detail))


class FakeMessage:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.sent.append({"text": text, "keyboard": reply_markup})
        return self

    async def edit_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append({"text": text, "keyboard": reply_markup})
        return self


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = 0
        self.edits = []

    async def answer(self, *a, **kw):
        self.answered += 1

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.edits.append({"text": text, "keyboard": reply_markup})
        self.message.edits.append({"text": text, "keyboard": reply_markup})


class FakeChat:
    def __init__(self, chat_id, kind="private"):
        self.id = chat_id
        self.type = kind


class FakeUpdate:
    def __init__(self, chat_id, data=None, kind="private"):
        self.effective_chat = FakeChat(chat_id, kind)
        self.effective_message = FakeMessage()
        self.message = self.effective_message
        self.callback_query = FakeQuery(data, self.effective_message) if data else None

    def press(self, data):
        self.callback_query = FakeQuery(data, self.effective_message)
        return self.callback_query


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.bot_data = {}
        self.args = []


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def keyboard_labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def keyboard_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def index_of(key):
    for i, (k, _label) in enumerate(S.TOPIC_CHOICES):
        if k == key:
            return i
    raise AssertionError("kind not offered: %r" % key)


def test_new_location_flow():
    chat = 801
    S.remove_all_subscriptions(chat)
    update = FakeUpdate(chat)
    ctx = FakeContext()
    ctx.user_data["pending"] = {
        "label": "Ljubljana", "lat": 46.0569, "lon": 14.5058,
        "radius_km": 10.0, "kind": "circle",
    }

    state = run(S._ask_topics(update, ctx, from_callback=False))
    check("picker asks next", state == S.TOPICS, repr(state))
    sent = update.effective_message.sent[-1]
    check("picker lists every kind",
          len([l for l in keyboard_labels(sent["keyboard"]) if l.strip()]) == len(S.TOPIC_CHOICES) + 2,
          repr(keyboard_labels(sent["keyboard"])))
    check("picker mentions the location", "Ljubljana" in sent["text"])
    check("picker starts with no kinds chosen", ctx.user_data["topic_selection"] == [])

    # choose Epidemije + Jedrska ... by pressing their buttons
    for key in ("Epidemije", "Jedrska ali radiološka nevarnost"):
        update.press("top:%d" % index_of(key))
        state = run(S.picked_topic(update, ctx))
        check("toggle keeps the picker open (%s)" % key, state == S.TOPICS)
    check("both kinds selected",
          ctx.user_data["topic_selection"] == ["Epidemije", "Jedrska ali radiološka nevarnost"],
          repr(ctx.user_data["topic_selection"]))
    check("checkmark appears on chosen kind",
          any(lab.startswith("✅") and "Epidemije" in lab
              for lab in keyboard_labels(update.callback_query.edits[-1]["keyboard"])))

    # pressing the same kind again clears it, then re-add once for the save
    update.press("top:%d" % index_of("Epidemije"))
    run(S.picked_topic(update, ctx))
    check("second press deselects", ctx.user_data["topic_selection"] == ["Jedrska ali radiološka nevarnost"])

    update.press("top:%d" % index_of("Epidemije"))
    run(S.picked_topic(update, ctx))
    update.press("top:save")
    state = run(S.picked_topic(update, ctx))
    check("save closes the conversation", state == S.ConversationHandler.END, repr(state))

    rows = S.list_subscriptions(chat)
    check("one location saved", len(rows) == 1, repr(rows))
    check("saved filter matches the picker",
          sorted(S.decode_topics(rows[0]["topics"]))
          == sorted(["Epidemije", "Jedrska ali radiološka nevarnost"]),
          repr(rows[0]["topics"]))
    check("confirmation names the kinds",
          "Epidemije" in update.effective_message.edits[-1]["text"]
          or "Epidemije" in update.effective_message.sent[-1]["text"])
    check("pending scratch is cleared", "pending" not in ctx.user_data, repr(ctx.user_data.keys()))


def test_skip_filter():
    chat = 802
    S.remove_all_subscriptions(chat)
    ctx = FakeContext()
    ctx.user_data["pending"] = {"label": "Maribor", "lat": 46.5547, "lon": 15.6459,
                                "radius_km": 5.0, "kind": "circle"}
    update = FakeUpdate(chat)
    run(S._ask_topics(update, ctx))
    update.press("top:save")
    run(S.picked_topic(update, ctx))
    rows = S.list_subscriptions(chat)
    check("saving with nothing chosen stores no filter", rows and rows[0]["topics"] is None,
          repr(rows[0]["topics"] if rows else rows))


def test_edit_existing():
    chat = 803
    S.remove_all_subscriptions(chat)
    sid = S.add_subscription(chat, "Kranj", 46.2397, 14.3567, 8, topics=["Gore"])
    ctx = FakeContext()
    update = FakeUpdate(chat)

    state = run(S.cmd_topics(update, ctx))
    check("/topics with one location opens the list", state == S.TOPIC_PICK, repr(state))
    listing = update.effective_message.sent[-1]
    check("/topics lists the location",
          any("Kranj" in lab for lab in keyboard_labels(listing["keyboard"])),
          repr(keyboard_labels(listing["keyboard"])))
    check("/topics list names the stored filter",
          any("Gore" in lab for lab in keyboard_labels(listing["keyboard"])),
          repr(keyboard_labels(listing["keyboard"])))
    check("/topics offers the location as a button",
          "tsel:%d" % sid in keyboard_data(listing["keyboard"]),
          repr(keyboard_data(listing["keyboard"])))

    update.press("tsel:%d" % sid)
    state = run(S.picked_topic_subscription(update, ctx))
    check("choosing a location opens the picker", state == S.TOPICS, repr(state))
    check("picker prefilled from the stored filter",
          ctx.user_data["topic_selection"] == ["Gore"], repr(ctx.user_data["topic_selection"]))

    update.press("top:%d" % index_of("Gore"))       # drop Gore
    run(S.picked_topic(update, ctx))
    update.press("top:%d" % index_of("Športne aktivnosti"))
    run(S.picked_topic(update, ctx))
    update.press("top:save")
    state = run(S.picked_topic(update, ctx))
    check("saving an edit ends the conversation", state == S.ConversationHandler.END, repr(state))
    check("stored filter updated",
          S.decode_topics(S.get_subscription(chat, sid)["topics"]) == ["Športne aktivnosti"],
          repr(S.get_subscription(chat, sid)["topics"]))
    check("location itself untouched", S.get_subscription(chat, sid)["label"] == "Kranj")


def test_reset_to_all():
    chat = 804
    S.remove_all_subscriptions(chat)
    sid = S.add_subscription(chat, "Celje", 46.2309, 15.2604, 6, topics=["Gore", "Epidemije"])
    ctx = FakeContext()
    update = FakeUpdate(chat)
    run(S.cmd_topics(update, ctx))
    update.press("tsel:%d" % sid)
    run(S.picked_topic_subscription(update, ctx))
    update.press("top:all")
    run(S.picked_topic(update, ctx))
    check("reset clears the selection", ctx.user_data["topic_selection"] == [])
    update.press("top:save")
    run(S.picked_topic(update, ctx))
    check("reset stores no filter", S.get_subscription(chat, sid)["topics"] is None,
          repr(S.get_subscription(chat, sid)["topics"]))


def test_group_refused():
    chat = -1001234
    update = FakeUpdate(chat, kind="group")
    ctx = FakeContext()
    result = run(S.cmd_topics(update, ctx))
    check("groups cannot open the picker",
          result in (None, S.ConversationHandler.END), repr(result))
    check("group gets an explanation", bool(update.effective_message.sent),
          repr(update.effective_message.sent))


def test_bad_callback():
    chat = 805
    S.remove_all_subscriptions(chat)
    update = FakeUpdate(chat, data="top:999")
    ctx = FakeContext()
    ctx.user_data["topic_selection"] = []
    state = run(S.picked_topic(update, ctx))
    check("unknown index just re-renders", state == S.TOPICS, repr(state))
    check("selection unchanged", ctx.user_data["topic_selection"] == [])


def main():
    for fn in (test_new_location_flow, test_skip_filter, test_edit_existing,
               test_reset_to_all, test_group_refused, test_bad_callback):
        print("\n== %s ==" % fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append("%s crashed: %r" % (fn.__name__, exc))
    print("\n%d passed, %d failed" % (PASSES, len(FAILS)))
    if FAILS:
        print("failed: " + ", ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
