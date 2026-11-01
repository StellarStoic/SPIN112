#!/usr/bin/env python3
"""Tests for the SPIN112 radius-subscription feature.

Run from the bot directory on the Pi:

    SUBSCRIPTIONS_DB=/tmp/spin_test.db python3 tests/test_radius.py

Uses a throwaway SQLite file, a fake bot object and a fake map renderer, so it
never touches the live database, Telegram, or the tile server.
"""
import asyncio
import os
import sys
import tempfile
import urllib.error

DB_TMP = os.path.join(tempfile.mkdtemp(prefix="spin_subs_"), "test_subs.db")
os.environ["SUBSCRIPTIONS_DB"] = DB_TMP

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)

import subscriptions as S  # noqa: E402
import SPIN112 as B  # noqa: E402

# Belt and braces: pin the throwaway DB even if the bot module was imported first.
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


class FakeBot:
    def __init__(self):
        self.photos = []
        self.texts = []

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **kw):
        data = photo.read() if hasattr(photo, "read") else open(photo, "rb").read()
        assert data, "empty photo payload"
        self.photos.append({"chat_id": chat_id, "bytes": len(data), "caption": caption})
        return True

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.texts.append({"chat_id": chat_id, "text": text})
        return True


INCIDENT = {
    "id": "999999",
    "pub_date": "Sun, 01 Nov 2026 07:36:16 GMT",
    "link_suffix": "test",
}
DETAILS = {
    "dogodekNaziv": "Požar v naravi",
    "besedilo": "Testni opis dogodka. " * 120,   # ~2600 chars, forces caption trimming
    "intervencijaVrstaNaziv": "Požar, eksplozija",
    "obcinaNaziv": "Ljubljana",
    "nastanekCas": "N/A",
    "wgsLatApprox": 46.0569,
    "wgsLonApprox": 14.5058,
}


def test_geometry():
    d = S.haversine_km(46.0569, 14.5058, 46.5547, 15.6459)   # Ljubljana -> Maribor
    check("haversine Ljubljana-Maribor ~105 km", 100 <= d <= 110, "got %.1f" % d)
    check("haversine zero distance", S.haversine_km(46.0, 14.0, 46.0, 14.0) < 0.001)


def test_store():
    check("db file created", os.path.exists(DB_TMP), DB_TMP)
    S.remove_all_subscriptions(111)
    S.remove_all_subscriptions(222)

    sid = S.add_subscription(111, "Ljubljana", 46.0569, 14.5058, 10)
    check("add_subscription returns id", bool(sid), repr(sid))
    subs = S.list_subscriptions(111)
    check("list_subscriptions sees the row", len(subs) == 1, repr(subs))

    inside = S.match_subscribers(46.0500, 14.5100)   # ~1 km from centre
    check("match inside radius", 111 in inside, repr(list(inside)))
    if 111 in inside:
        dist = inside[111][0][1]
        check("match reports distance", 0 < dist < 5, "got %.2f km" % dist)

    outside = S.match_subscribers(46.5547, 15.6459)  # Maribor, ~105 km away
    check("no match outside radius", 111 not in outside, repr(list(outside)))

    S.add_subscription(222, "Maribor", 46.5547, 15.6459, 5)
    mb = S.match_subscribers(46.5547, 15.6459)
    check("only the local chat matches", list(mb) == [222], repr(list(mb)))
    lj = S.match_subscribers(46.0569, 14.5058)
    check("chat 111 still matches its own area", 111 in lj, repr(list(lj)))

    S.record_delivery(111, "999999")
    check("already_delivered true after record", S.already_delivered(111, "999999"))
    check("already_delivered false for other chat", not S.already_delivered(222, "999999"))
    check("already_delivered false for other incident", not S.already_delivered(111, "999998"))

    S.remove_subscription(111, subs[0][0] if isinstance(subs[0], (list, tuple)) else subs[0]["id"])
    check("remove_subscription empties the list", S.list_subscriptions(111) == [], repr(S.list_subscriptions(111)))
    S.remove_all_subscriptions(222)
    check("remove_all_subscriptions empties the list", S.list_subscriptions(222) == [])


def test_geocoding():
    lj = S.geocode_municipalities("Ljubljana")
    check("offline index resolves Ljubljana", bool(lj), repr(lj))
    if lj:
        check("Ljubljana label", "ljubljana" in lj[0]["label"].lower(), repr(lj[0]))
        check("Ljubljana coords", 45.9 < lj[0]["lat"] < 46.2 and 14.3 < lj[0]["lon"] < 14.8, repr(lj[0]))
    ptuj = S.geocode_municipalities("Ptuj")
    check("offline index resolves Ptuj", bool(ptuj), repr(ptuj))

    try:
        live = S.geocode_nominatim("Videm pri Ptuju")
    except (urllib.error.URLError, OSError) as exc:
        print("SKIP  nominatim village lookup (%s)" % exc)
        return
    check("nominatim resolves a village", bool(live), repr(live[:2]))
    if live:
        check("village candidate has coordinates",
              bool(live[0].get("lat")) and bool(live[0].get("lon")), repr(live[0]))


def test_handlers():
    handlers = S.build_handlers()
    check("build_handlers returns handlers", len(handlers) >= 5, "got %d" % len(handlers))
    from telegram.ext import ConversationHandler
    check("a ConversationHandler is registered",
          any(isinstance(h, ConversationHandler) for h in handlers),
          repr([type(h).__name__ for h in handlers]))


def test_message():
    message, caption = B.compose_incident(DETAILS, INCIDENT)
    check("full message longer than caption", len(message) >= len(caption))
    check("caption under Telegram limit", len(caption) <= B.TELEGRAM_CAPTION_LIMIT,
          "got %d" % len(caption))
    for tag in ("<b>", "</b>", "<i>", "</i>", "<a href"):
        check("caption keeps %s" % tag, caption.count(tag) > 0)
    check("trimmed caption keeps the ellipsis", "\u2026" in caption, repr(caption[-80:]))
    _, tighter = B.compose_incident(DETAILS, INCIDENT, extra_trim=200)
    check("extra_trim shortens further", len(tighter) < len(caption),
          "%d vs %d" % (len(tighter), len(caption)))


def test_delivery():
    S.remove_all_subscriptions(333)
    S.remove_all_subscriptions(444)
    S.add_subscription(333, "Ljubljana", 46.0569, 14.5058, 10)   # inside
    S.add_subscription(444, "Maribor", 46.5547, 15.6459, 5)      # outside

    real_map = B.create_static_map_image

    def fake_map(lat, lon, filename="incident_map.png", **kw):
        with open(filename, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    B.create_static_map_image = fake_map
    try:
        bot = FakeBot()
        asyncio.get_event_loop().run_until_complete(
            B.notify_radius_subscribers(bot, INCIDENT, DETAILS))
        check("one photo delivered", len(bot.photos) == 1, repr(bot.photos))
        if bot.photos:
            photo = bot.photos[0]
            check("delivered to the subscriber chat", photo["chat_id"] == 333, repr(photo["chat_id"]))
            check("photo payload non-empty", photo["bytes"] > 0)
            check("caption carries the match note", "Ljubljana" in (photo["caption"] or ""),
                  repr((photo["caption"] or "")[-120:]))
            check("caption under limit after note", len(photo["caption"] or "") <= B.TELEGRAM_CAPTION_LIMIT,
                  "got %d" % len(photo["caption"] or ""))
        check("no text fallback needed", bot.texts == [], repr(bot.texts))
        check("subscriber recorded", S.already_delivered(333, "999999"))
        check("outsider not recorded", not S.already_delivered(444, "999999"))

        bot2 = FakeBot()
        asyncio.get_event_loop().run_until_complete(
            B.notify_radius_subscribers(bot2, INCIDENT, DETAILS))
        check("second run sends nothing (dedupe)", bot2.photos == [] and bot2.texts == [],
              repr(bot2.photos) + repr(bot2.texts))
    finally:
        B.create_static_map_image = real_map
        for path in ("subscriber_map.png",):
            if os.path.exists(path):
                os.remove(path)


def main():
    for fn in (test_geometry, test_store, test_geocoding, test_handlers,
               test_message, test_delivery):
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
