"""/contact has to reach the developer, from the menu, the help text and the reply.

The command exists so a user who finds a bug can write to a human without
digging through the group: the same handle appears in the /help text, in
Telegram's command menu and in the answer to /contact.
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
CONTACT_HANDLE = "@StellarStoic"

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


def test_contact_url_is_the_dev_handle():
    assert S.DEV_CONTACT_URL == CONTACT_URL, S.DEV_CONTACT_URL
    assert S.DEV_CONTACT_HANDLE == CONTACT_HANDLE, S.DEV_CONTACT_HANDLE


def test_reply_links_the_dev():
    text, kwargs = _reply()
    assert text, "handler replied with nothing"
    assert CONTACT_URL in text, text
    assert CONTACT_HANDLE in text, text
    assert '<a href="' + CONTACT_URL + '">' in text, text
    assert kwargs.get("parse_mode") == "HTML", kwargs
    assert kwargs.get("disable_web_page_preview") is True, kwargs


def test_help_mentions_the_contact_command():
    assert "/contact" in S.HELP_TEXT
    assert CONTACT_URL in S.HELP_TEXT
    assert CONTACT_HANDLE in S.HELP_TEXT


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
