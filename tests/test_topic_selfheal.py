"""Topic self-heal: a deleted Telegram topic must not silently swallow posts.

When a topic is deleted the API answers "Bad Request: message thread not
found". The bot now tries to recreate the topic under the same name (needs the
"Manage topics" admin right), persists the fresh message_thread_id and posts
into it; if recreation is refused it logs and skips as before.

Runs offline with a fake bot.

    TELEGRAM_BOT_TOKEN=1:T TELEGRAM_GROUP_ID=-1001 python tests/test_topic_selfheal.py
"""

import asyncio
import json
import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_GROUP_ID", "-1001234567890")
os.environ["SUBSCRIPTIONS_DB"] = os.path.join(BOT_DIR, "tests", "_selfheal_tmp.db")
sys.path.insert(0, BOT_DIR)

from telegram.error import BadRequest, TelegramError  # noqa: E402

import SPIN112 as B  # noqa: E402

PASSES = []
FAILS = []
DB_PATH = os.path.join(BOT_DIR, "tests", "_selfheal_tmp.db")
TMP_IDS = os.path.join(BOT_DIR, "tests", "_topic_ids_tmp.json")
TMP_PHOTO = os.path.join(BOT_DIR, "tests", "_selfheal_tmp.png")
NAME = "Prva prijava-Nepreverjen dogodek"
DEAD_ID = 3125
NEW_ID = 4444
_ORIGINAL_TOPICS = dict(B.topics)


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print("PASS", name)
    else:
        FAILS.append(name)
        print("FAIL", name, detail)


def reset():
    """Restore module state and remove the state file between cases."""
    B.topics.clear()
    B.topics.update(_ORIGINAL_TOPICS)
    B._topic_id_overrides = {}
    B.TOPIC_IDS_FILE = TMP_IDS
    if os.path.exists(TMP_IDS):
        os.remove(TMP_IDS)


class CreatedTopic:
    def __init__(self, thread_id, name):
        self.message_thread_id = thread_id
        self.name = name


class FakeBot:
    """Accepts only NEW_ID as a thread id; can create the topic or refuse."""

    username = "nasoncnistranialpbot"
    id = 7800876684

    def __init__(self, allow_create=True, valid_ids=(NEW_ID,), alive_ids=()):
        self.allow_create = allow_create
        self.valid_ids = set(valid_ids)
        self.alive_ids = set(alive_ids)
        self.sent = []
        self.created = []
        self.create_calls = 0

    async def edit_forum_topic(self, chat_id, message_thread_id, name=None):
        if message_thread_id in self.alive_ids:
            return True
        if message_thread_id not in self.valid_ids:
            raise BadRequest("Bad Request: message thread not found")
        return True

    async def create_forum_topic(self, chat_id, name):
        self.create_calls += 1
        if not self.allow_create:
            raise TelegramError("Bad Request: not enough rights to manage topics")
        self.created.append(name)
        self.valid_ids.add(NEW_ID)
        return CreatedTopic(NEW_ID, name)

    async def send_message(self, chat_id, text, parse_mode=None, message_thread_id=None):
        if message_thread_id not in self.valid_ids:
            raise BadRequest("Bad Request: message thread not found")
        self.sent.append(("message", message_thread_id))
        return ("msg", message_thread_id)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, message_thread_id=None):
        if message_thread_id not in self.valid_ids:
            raise BadRequest("Bad Request: message thread not found")
        self.sent.append(("photo", message_thread_id))
        return ("photo", message_thread_id)


def test_topic_name_lookup():
    reset()
    check("known id maps back to its topic name", B.topic_name_for_id(DEAD_ID) == NAME,
          repr(B.topic_name_for_id(DEAD_ID)))
    check("main-chat id (None) has no name", B.topic_name_for_id(None) is None)
    check("unknown id has no name", B.topic_name_for_id(999999) is None)


def test_deleted_topic_is_recreated_and_post_lands():
    reset()
    bot = FakeBot(allow_create=True)
    result = asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=DEAD_ID))
    check("post is delivered after recreate", result == ("msg", NEW_ID), repr(result))
    check("topic was recreated once", bot.created == [NAME], repr(bot.created))
    check("live map points at the new id", B.topics[NAME] == NEW_ID, repr(B.topics[NAME]))
    check("message went into the new thread", bot.sent == [("message", NEW_ID)], repr(bot.sent))
    check("new id persisted to state file", os.path.exists(TMP_IDS))
    data = json.load(open(TMP_IDS, encoding="utf-8")) if os.path.exists(TMP_IDS) else {}
    check("state file holds the new id", data.get(NAME) == NEW_ID, repr(data))


def test_photo_post_is_healed_too():
    reset()
    with open(TMP_PHOTO, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\nfake")
    bot = FakeBot(allow_create=True)
    result = asyncio.run(B.retry_send_photo(bot, -1001, TMP_PHOTO, "cap", message_thread_id=DEAD_ID))
    check("photo post is delivered after recreate", result == ("photo", NEW_ID), repr(result))
    check("photo topic was recreated", bot.created == [NAME], repr(bot.created))
    check("photo landed in the new thread", bot.sent == [("photo", NEW_ID)], repr(bot.sent))
    os.remove(TMP_PHOTO)


def test_recreate_without_right_degrades_to_skip():
    reset()
    bot = FakeBot(allow_create=False)
    result = asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=DEAD_ID))
    check("no delivery when recreation is refused", result is None, repr(result))
    check("recreation was attempted once", bot.create_calls == 1, repr(bot.create_calls))
    check("nothing was sent", bot.sent == [], repr(bot.sent))
    check("map untouched", B.topics[NAME] == DEAD_ID, repr(B.topics[NAME]))
    check("no state file written", not os.path.exists(TMP_IDS))


def test_unknown_thread_is_not_turned_into_a_topic():
    reset()
    bot = FakeBot(allow_create=True)
    result = asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=999999))
    check("unknown thread id is skipped", result is None, repr(result))
    check("no topic created for an unmapped id", bot.create_calls == 0, repr(bot.create_calls))


def test_healthy_topic_is_not_recreated():
    reset()
    bot = FakeBot(allow_create=True, valid_ids=(NEW_ID, 3))
    result = asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=3))
    check("existing topic posts normally", result == ("msg", 3), repr(result))
    check("no recreate call for a live topic", bot.create_calls == 0, repr(bot.create_calls))


def test_duplicate_topic_is_not_created_when_original_still_exists():
    reset()
    # send failed, but a topic with that id still exists -> recreate would duplicate it
    bot = FakeBot(allow_create=True, alive_ids=(DEAD_ID,))
    result = asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=DEAD_ID))
    check("a still-existing topic is never duplicated", bot.create_calls == 0, repr(bot.create_calls))
    check("no delivery on an unexplained thread error", result is None, repr(result))


def test_overrides_survive_a_restart():
    reset()
    bot = FakeBot(allow_create=True)
    asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=DEAD_ID))
    # simulate a process restart: hardcoded map, then load the state file
    B.topics.clear()
    B.topics.update(_ORIGINAL_TOPICS)
    B._topic_id_overrides = {}
    check("hardcoded map is back before loading", B.topics[NAME] == DEAD_ID, repr(B.topics[NAME]))
    B.load_topic_overrides()
    check("override is reapplied on startup", B.topics[NAME] == NEW_ID, repr(B.topics[NAME]))
    result = asyncio.run(B.retry_send_message(bot, -1001, "hi", message_thread_id=B.topics[NAME]))
    check("posting works right after restart", result == ("msg", NEW_ID), repr(result))


def test_broken_state_file_is_ignored():
    reset()
    with open(TMP_IDS, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    B.load_topic_overrides()
    check("corrupt state file leaves the map intact", B.topics[NAME] == DEAD_ID, repr(B.topics[NAME]))


def test_state_file_rejects_nonsense_entries():
    reset()
    with open(TMP_IDS, "w", encoding="utf-8") as fh:
        json.dump({NAME: NEW_ID, "NoSuchTopic": 5, "POMURSKA": "abc", "PODRAVSKA": 0}, fh)
    B.load_topic_overrides()
    check("valid entry applied", B.topics[NAME] == NEW_ID, repr(B.topics[NAME]))
    check("string value rejected", B.topics["POMURSKA"] == 3, repr(B.topics["POMURSKA"]))
    check("zero value rejected", B.topics["PODRAVSKA"] == 4, repr(B.topics["PODRAVSKA"]))
    check("unknown topic name ignored", "NoSuchTopic" not in B.topics)


def _cleanup():
    for path in (TMP_IDS, TMP_PHOTO):
        try:
            os.remove(path)
        except OSError:
            pass
    for suffix in ("", "-journal"):
        try:
            os.remove(DB_PATH + suffix)
        except OSError:
            pass


def run_all():
    tests = [f for f in globals().values() if callable(f) and getattr(f, "__name__", "").startswith("test_")]
    try:
        for fn in sorted(tests, key=lambda f: f.__name__):
            fn()
        reset()
    finally:
        _cleanup()
    print()
    print(f"{len(PASSES)} passed, {len(FAILS)} failed")
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_all())
