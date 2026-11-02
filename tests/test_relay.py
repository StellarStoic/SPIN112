"""Plain messages to the bot are relayed to the developer, with who sent them.

"Just write to the bot" has to work without a command: any private-chat text (or
photo, voice note …) is copied to the developer's own chat behind the
DEV_CHAT_ID environment variable, and the user gets a short confirmation that
reveals nothing about the developer. The handler is registered in a later group
so /track and friends still own their updates.
"""

import asyncio
import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_GROUP_ID", "-1001234567890")
os.environ["SUBSCRIPTIONS_DB"] = os.path.join(BOT_DIR, "tests", "_relay_tmp.db")

import subscriptions as S  # noqa: E402

DEV_ID = 1471594734
PASSES = []
FAILS = []


class _User:
    def __init__(self, uid=555001, username="ana", first_name="Ana", last_name="Novak"):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class _Chat:
    type = "private"
    id = 555001


class _Message:
    def __init__(self, text="Zakaj nisem dobil obvestila za Vrhniko?", photo=None):
        self.text = text
        self.caption = None
        self.photo = photo
        self.chat = _Chat()
        self.replies = []
        self.forwarded = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def forward(self, chat_id):
        self.forwarded.append(chat_id)


class _Update:
    def __init__(self, message=None, user=None):
        self.effective_message = message or _Message()
        self.effective_user = user or _User()


class _Bot:
    def __init__(self):
        self.sent = []
        self.fail = None

    async def send_message(self, **kwargs):
        if self.fail:
            raise S.TelegramError(self.fail)
        self.sent.append(kwargs)


class _Context:
    def __init__(self, bot=None):
        self.bot = bot or _Bot()


def _run(update, context=None, dev=DEV_ID):
    if dev:
        os.environ["DEV_CHAT_ID"] = str(dev)
    else:
        os.environ.pop("DEV_CHAT_ID", None)
    context = context or _Context()
    asyncio.run(S.cmd_user_message(update, context))
    return context


def test_dev_chat_id_reads_env():
    os.environ["DEV_CHAT_ID"] = str(DEV_ID)
    assert S.dev_chat_id() == DEV_ID
    os.environ["DEV_CHAT_ID"] = "  "
    assert S.dev_chat_id() == 0
    os.environ["DEV_CHAT_ID"] = "nic"
    assert S.dev_chat_id() == 0
    os.environ.pop("DEV_CHAT_ID", None)
    assert S.dev_chat_id() == 0


def test_text_is_relayed_with_sender_details():
    update = _Update()
    context = _run(update)
    assert len(context.bot.sent) == 1, context.bot.sent
    sent = context.bot.sent[0]
    assert sent["chat_id"] == DEV_ID, sent
    body = sent["text"]
    assert "Ana Novak" in body, body
    assert "@ana" in body, body
    assert "555001" in body, body
    assert "Zakaj nisem dobil obvestila za Vrhniko?" in body, body
    assert sent["parse_mode"] == "HTML", sent


def test_user_gets_a_confirmation_that_names_nobody():
    update = _Update()
    _run(update)
    assert update.effective_message.replies, "user got no answer"
    text = update.effective_message.replies[0][0]
    assert "posredoval" in text.lower(), text
    assert "stellarstoic" not in text.lower()
    assert "t.me/" not in text.lower()
    assert str(DEV_ID) not in text


def test_html_in_user_text_is_escaped():
    update = _Update(_Message(text="<b>pomoc</b> & <script>"))
    context = _run(update)
    body = context.bot.sent[0]["text"]
    assert "<b>pomoc</b>" not in body, body
    assert "&lt;b&gt;pomoc&lt;/b&gt;" in body, body
    assert "&amp;" in body, body


def test_media_is_forwarded_and_summarised():
    update = _Update(_Message(text=None, photo=[object()]))
    context = _run(update)
    sent = context.bot.sent[0]
    assert "[fotografija]" in sent["text"], sent["text"]
    assert update.effective_message.forwarded == [DEV_ID], update.effective_message.forwarded


def test_long_text_is_truncated():
    update = _Update(_Message(text="x" * (S.MAX_RELAY_CHARS + 500)))
    context = _run(update)
    body = context.bot.sent[0]["text"]
    assert body.count("x") == S.MAX_RELAY_CHARS, body.count("x")


def test_developer_own_messages_are_not_relayed():
    update = _Update(_Message(text="test"), user=_User(uid=DEV_ID, username="dev"))
    context = _run(update)
    assert context.bot.sent == [], context.bot.sent
    assert update.effective_message.replies == [], update.effective_message.replies


def test_without_dev_chat_id_the_user_is_pointed_at_contact():
    update = _Update()
    context = _run(update, dev=None)
    assert context.bot.sent == [], context.bot.sent
    text = update.effective_message.replies[0][0]
    assert "/contact" in text, text


def test_relay_failure_does_not_claim_success():
    update = _Update()
    bot = _Bot()
    bot.fail = "Forbidden: bot was blocked by the user"
    context = _run(update, _Context(bot))
    text = update.effective_message.replies[0][0]
    assert "✅" not in text, text
    assert "/contact" in text, text


def test_handler_is_a_private_chat_catch_all():
    from telegram import Update

    handlers = S.build_relay_handlers()
    assert len(handlers) == 1, handlers
    handler = handlers[0]
    assert handler.callback is S.cmd_user_message, handler.callback

    def _update(text, entities=None):
        message = {
            "message_id": 1,
            "date": 1730000000,
            "chat": {"id": 555001, "type": "private"},
            "from": {"id": 555001, "is_bot": False, "first_name": "Ana"},
            "text": text,
        }
        if entities:
            message["entities"] = entities
        return Update.de_json({"update_id": 1, "message": message}, None)

    assert handler.check_update(_update("kaj se dogaja?")), "plain text must be caught"
    command = _update("/start", [{"type": "bot_command", "offset": 0, "length": 6}])
    assert not handler.check_update(command), "commands belong to group 0"


def test_spin112_registers_the_relay_group():
    import io
    source = io.open(os.path.join(BOT_DIR, "SPIN112.py"), encoding="utf-8").read()
    assert "build_relay_handlers" in source
    assert "group=1" in source


def _cleanup():
    for suffix in ("", "-journal"):
        try:
            os.remove(os.path.join(BOT_DIR, "tests", "_relay_tmp.db") + suffix)
        except OSError:
            pass


def run_all():
    tests = [f for f in globals().values()
             if callable(f) and getattr(f, "__name__", "").startswith("test_")]
    try:
        for fn in sorted(tests, key=lambda f: f.__name__):
            try:
                fn()
                PASSES.append(fn.__name__)
                print(f"PASS {fn.__name__}")
            except AssertionError as exc:
                FAILS.append(fn.__name__)
                print(f"FAIL {fn.__name__} {exc}")
    finally:
        _cleanup()
    print()
    print(f"{len(PASSES)} passed, {len(FAILS)} failed")


if __name__ == "__main__":
    run_all()
