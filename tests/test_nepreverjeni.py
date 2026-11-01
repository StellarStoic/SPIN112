#!/usr/bin/env python3
"""Tests for the "Prva prijava" (unverified first report) flow.

Run from the bot directory on the Pi:

    python3 tests/test_nepreverjeni.py

Everything is faked except the module itself: no Telegram, no tile server, no
HTTP. The state files live in a throwaway directory.
"""
import asyncio
import json
import os
import sys
import tempfile

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)

STATE_DIR = tempfile.mkdtemp(prefix="spin_nepr_")
os.environ.setdefault("SUBSCRIPTIONS_DB", os.path.join(STATE_DIR, "subs.db"))

import SPIN112 as B  # noqa: E402

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
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.sent.append({"chat_id": chat_id, "thread": kw.get("message_thread_id")})
        return FakeMessage(1000 + len(self.sent))

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **kw):
        self.sent.append({"chat_id": chat_id, "thread": kw.get("message_thread_id")})
        return FakeMessage(2000 + len(self.sent))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


def fake_incident(incident_id):
    return {
        "id": incident_id,
        "link_suffix": "/incident/%s" % incident_id,
        "title": "Test %s" % incident_id,
        "description": "Test report %s" % incident_id,
        "naziv": "Test %s" % incident_id,
        "date": "2026-11-01T10:00:00",
        "region": "OSREDNJESLOVENSSKA",
        "obcinaNaziv": "Ljubljana",
        "dogodekNaziv": "Testni dogodek",
    }


def install_fakes(details_by_id, feed_ids):
    """Fake RSS feed, detail fetch and the topic-posting call."""
    B.get_rss_feed = lambda url: "<rss/>"
    B.parse_rss_feed = lambda content: [fake_incident(i) for i in feed_ids]

    def fake_details(link_suffix):
        incident_id = link_suffix.rsplit("/", 1)[-1]
        if incident_id not in details_by_id:
            return None
        return {"value": details_by_id[incident_id]}

    B.get_incident_details = fake_details
    posts = []

    async def fake_post(bot, incident, topic_id, is_verified=True):
        posts.append({"id": incident["id"], "topic": topic_id, "verified": is_verified,
                      "chat": B.TELEGRAM_GROUP_ID})
        return FakeMessage(int(incident["id"]))

    B.post_incident_to_topic = fake_post
    return posts


def unverified_details():
    return {"besedilo": "N/A", "intervencijaVrstaNaziv": "N/A",
            "wgsLatApprox": 46.05, "wgsLonApprox": 14.5}


def verified_details():
    return {"besedilo": "Požar v kleti, gasilci na poti.", "intervencijaVrstaNaziv": "Požar, eksplozija",
            "wgsLatApprox": 46.05, "wgsLonApprox": 14.5}


def state_paths(tag):
    d = os.path.join(STATE_DIR, tag)
    os.makedirs(d, exist_ok=True)
    B.posted_nepreverjeni_incidents_file = os.path.join(d, "posted_nepreverjeni_incidents.json")
    B.posted_nepreverjeni_messages_file = os.path.join(d, "posted_nepreverjeni_messages.json")
    return B.posted_nepreverjeni_incidents_file, B.posted_nepreverjeni_messages_file


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def main():
    B.TELEGRAM_GROUP_ID = "-1001234567890"

    # --- topics table -------------------------------------------------------
    check("unverified topic present", B.topics.get("Prva prijava-Nepreverjen dogodek") == 3125,
          B.topics.get("Prva prijava-Nepreverjen dogodek"))
    check("region topics kept", B.topics.get("POMURSKA") == 3, B.topics.get("POMURSKA"))
    check("vecji obseg topic kept", B.topics.get("Večji obseg") == 1404,
          B.topics.get("Večji obseg"))

    # --- first run only seeds ----------------------------------------------
    state, messages = state_paths("seed")
    posts = install_fakes({"1": unverified_details(), "2": unverified_details()}, ["1", "2"])
    ctx = FakeContext()
    run(B.auto_fetch_and_post_nepreverjeni(ctx))
    check("first run posts nothing", posts == [], posts)
    check("first run seeds state file", os.path.exists(state))
    seeded = json.load(open(state, encoding="utf-8")) if os.path.exists(state) else []
    check("seed holds both ids", sorted(seeded) == ["1", "2"], seeded)

    # --- second run with one new report ------------------------------------
    posts = install_fakes({"1": unverified_details(), "2": unverified_details(),
                           "3": unverified_details()}, ["1", "3", "2"])
    run(B.auto_fetch_and_post_nepreverjeni(ctx))
    check("only the new id posted", [p["id"] for p in posts] == ["3"], posts)
    check("posted to the unverified topic", posts and posts[0]["topic"] == 3125, posts)
    check("posted as unverified", posts and posts[0]["verified"] is False, posts)
    check("message id recorded", os.path.exists(messages) and
          json.load(open(messages, encoding="utf-8")).get("3", {}).get("message_id") == 3,
          json.load(open(messages, encoding="utf-8")) if os.path.exists(messages) else "missing")
    check("state file grew", "3" in json.load(open(state, encoding="utf-8")))

    # --- nothing new -> nothing posted -------------------------------------
    posts = install_fakes({"1": unverified_details(), "2": unverified_details(),
                           "3": unverified_details()}, ["1", "3", "2"])
    run(B.auto_fetch_and_post_nepreverjeni(ctx))
    check("repeat run is silent (no reposting)", posts == [], posts)

    # --- verified report is not announced ---------------------------------
    state, messages = state_paths("verified")
    posts = install_fakes({"9": verified_details()}, ["9"])
    run(B.auto_fetch_and_post_nepreverjeni(ctx, initial_run=True))
    check("seed run for verified id posts nothing", posts == [], posts)
    posts = install_fakes({"9": verified_details(), "10": verified_details()}, ["9", "10"])
    run(B.auto_fetch_and_post_nepreverjeni(ctx))
    check("verified report skipped", posts == [], posts)
    check("verified id marked handled",
          "10" in json.load(open(state, encoding="utf-8")),
          json.load(open(state, encoding="utf-8")))
    posts = install_fakes({"10": verified_details()}, ["10"])
    run(B.auto_fetch_and_post_nepreverjeni(ctx))
    check("verified id not re-fetched on later runs",
          posts == [], posts)

    # --- empty feed is harmless -------------------------------------------
    posts = install_fakes({}, [])
    run(B.auto_fetch_and_post_nepreverjeni(ctx))
    check("empty feed posts nothing", posts == [], posts)

    # --- store_message_id is idempotent -----------------------------------
    state, messages = state_paths("store")
    B.store_message_id("42", 555, "-1001234567890", messages)
    B.store_message_id("42", 556, "-1001234567890", messages)
    data = json.load(open(messages, encoding="utf-8"))
    check("store_message_id overwrites one key", data == {"42": {"message_id": 556,
                                                                "chat_id": "-1001234567890"}}, data)

    print()
    print("%d passed, %d failed" % (PASSES, len(FAILS)))
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
