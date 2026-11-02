"""/contact reaches the developer through a button, without naming anyone.

The reply text stays anonymous ("razvijalec bota"): the account behind it is an
implementation detail, so this test asserts the URL lives *only* inside the
inline button and never in the message text or in the help text.
"""

import asyncio
import io
import os
import re
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_GROUP_ID", "-1001234567890")
os.environ["SUBSCRIPTIONS_DB"] = os.path.join(BOT_DIR, "tests", "_contact_tmp.db")

import subscriptions as S  # noqa: E402

CONTACT_URL = "https://t.me/stellarstoic"

PASSES = []
FAILS = []


class _Message:
    def __init__(self):
        self.text = None
        self.kwargs = {}

    async def reply_text(self, text, **kwargs):
        self.text = text
        self.kwargs = kwargs


class _Update:
    def __init__(self):
        self.effective_message = _Message()


def _reply():
    update = _Update()
    asyncio.run(S.cmd_contact(update, None))
    return update.effective_message.text, update.effective_message.kwargs


def _buttons(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


def test_contact_url_is_configured():
    assert S.DEV_CONTACT_URL == CONTACT_URL, S.DEV_CONTACT_URL


def test_reply_offers_a_url_button():
    text, kwargs = _reply()
    assert text, "handler replied with nothing"
    markup = kwargs.get("reply_markup")
    assert markup is not None, kwargs
    buttons = _buttons(markup)
    assert len(buttons) == 1, buttons
    assert buttons[0].url == CONTACT_URL, buttons[0].url
    assert buttons[0].text, "button needs a label"
    assert kwargs.get("parse_mode") == "HTML", kwargs


def test_reply_does_not_name_the_developer():
    text, kwargs = _reply()
    assert isinstance(text, str) and text
    lowered = text.lower()
    assert CONTACT_URL not in text
    assert "stellarstoic" not in lowered
    assert "razvijal" in lowered, text
    # the URL survives only inside the button
    assert _buttons(kwargs["reply_markup"])[0].url == CONTACT_URL


def test_help_mentions_the_command_without_the_handle():
    assert "/contact" in S.HELP_TEXT
    assert "stellarstoic" not in S.HELP_TEXT.lower()
    assert CONTACT_URL not in S.HELP_TEXT
    assert "kontakt razvijalca" in S.HELP_TEXT.lower()


def test_handler_is_registered():
    commands = []
    for handler in S.build_handlers():
        commands.extend(getattr(handler, "commands", []) or [])
    assert "contact" in commands, commands


def test_menu_entry_exists():
    source = io.open(os.path.join(BOT_DIR, "SPIN112.py"), encoding="utf-8").read()
    assert 'BotCommand("contact"' in source
    # the command list is registered at startup, not per message
    assert re.search(r'BotCommand\("contact", "[^"]+"\)', source)


def _cleanup():
    for suffix in ("", "-journal"):
        try:
            os.remove(os.path.join(BOT_DIR, "tests", "_contact_tmp.db") + suffix)
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
