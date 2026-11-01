#!/usr/bin/env python3
"""Tests for the SPIN112 per-location incident-kind (topic) filter.

Run from the bot directory on the Pi:

    python3 tests/test_topic.py

Uses a throwaway SQLite file and a fake bot, so it never touches the live
database, Telegram, or the tile server.
"""
import asyncio
import os
import sys
import tempfile

DB_TMP = os.path.join(tempfile.mkdtemp(prefix="spin_topic_"), "test_topics.db")
os.environ["SUBSCRIPTIONS_DB"] = DB_TMP

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)

import subscriptions as S  # noqa: E402
import SPIN112 as B  # noqa: E402

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
    "id": "888888",
    "pub_date": "Sun, 01 Nov 2026 09:00:00 GMT",
    "link_suffix": "testtopic",
}
DETAILS_FIRE = {
    "dogodekNaziv": "Požar v naravi",
    "besedilo": "Testni opis dogodka. " * 40,
    "intervencijaVrstaNaziv": "Požar, eksplozija",
    "obcinaNaziv": "Ljubljana",
    "nastanekCas": "N/A",
    "wgsLatApprox": 46.0569,
    "wgsLonApprox": 14.5058,
}
DETAILS_EPIDEMIC = dict(DETAILS_FIRE, intervencijaVrstaNaziv="Epidemije",
                        dogodekNaziv="Epidemija gripe")


def test_vocabulary():
    keys = list(S.TOPIC_KEYS)
    check("15 kinds offered", len(keys) == 15, "got %d" % len(keys))
    check("keys are unique", len(set(keys)) == len(keys))
    check("labels cover every key", set(S.TOPIC_LABELS) == set(keys))
    for wanted in ("Epidemije", "Jedrska ali radiološka nevarnost", "Športne aktivnosti"):
        check("kind present: %s" % wanted, wanted in keys, repr(keys))


def test_encode_decode():
    check("empty selection means every kind", S.encode_topics([]) is None)
    check("None selection means every kind", S.encode_topics(None) is None)
    check("picking every kind collapses to no filter",
          S.encode_topics(S.TOPIC_KEYS) is None)

    two = S.encode_topics(["Epidemije", "Gore"])
    check("selection stored", two == "Epidemije|Gore", repr(two))
    check("selection decodes", S.decode_topics(two) == ["Epidemije", "Gore"],
          repr(S.decode_topics(two)))
    check("duplicates collapse", S.decode_topics(S.encode_topics(["Gore", "Gore"])) == ["Gore"])
    check("unknown names are dropped", S.decode_topics("Gore|Izmišljena vrsta") == ["Gore"])
    check("garbage decodes to nothing", S.decode_topics("") == [] and S.decode_topics(None) == [])

    check("describe shows the single kind",
          S.describe_topics({"topics": S.encode_topics(["Gore"])}) == "Gore")
    check("describe collapses a long list",
          S.describe_topics({"topics": S.encode_topics(["Gore", "Epidemije", "Požar, eksplozija"])})
          == "3 vrst")
    check("describe without filter", S.describe_topics({"topics": None}) == "vse vrste")
    check("describe tolerates a missing column", S.describe_topics({}) == "vse vrste")


def test_store():
    for chat in (701, 702, 703):
        S.remove_all_subscriptions(chat)

    sid = S.add_subscription(701, "Ljubljana", 46.0569, 14.5058, 10,
                             topics=["Epidemije", "Jedrska ali radiološka nevarnost"])
    row = S.get_subscription(701, sid)
    check("stored filter survives a round trip",
          S.decode_topics(row["topics"]) == ["Epidemije", "Jedrska ali radiološka nevarnost"],
          repr(row.get("topics")))
    check("row keeps the location label", row["label"] == "Ljubljana", repr(row["label"]))

    S.add_subscription(702, "Ljubljana širše", 46.0569, 14.5058, 10)   # no filter
    row2 = S.get_subscription(702, S.list_subscriptions(702)[0]["id"])
    check("location without a filter stores NULL", row2["topics"] is None, repr(row2["topics"]))

    check("set_topics updates the filter",
          S.set_topics(701, sid, ["Športne aktivnosti"]))
    check("set_topics took effect",
          S.decode_topics(S.get_subscription(701, sid)["topics"]) == ["Športne aktivnosti"],
          repr(S.get_subscription(701, sid)["topics"]))
    check("set_topics to empty clears the filter", S.set_topics(701, sid, []))
    check("filter cleared", S.get_subscription(701, sid)["topics"] is None)
    check("set_topics on a foreign subscription changes nothing",
          not S.set_topics(703, sid, ["Gore"]))
    check("re-adding the same label keeps the new filter",
          S.add_subscription(701, "Ljubljana", 46.0569, 14.5058, 10, topics=["Gore"])
          and S.decode_topics(S.get_subscription(701, sid)["topics"]) == ["Gore"])


def test_matching():
    for chat in (711, 712, 713):
        S.remove_all_subscriptions(chat)
    S.add_subscription(711, "Filtrirano", 46.0569, 14.5058, 10, topics=["Epidemije"])
    S.add_subscription(712, "Vse vrste", 46.0569, 14.5058, 10)
    S.add_subscription(713, "Druga vrsta", 46.0569, 14.5058, 10, topics=["Gore"])

    epi = S.match_subscribers(46.0569, 14.5058, {"Epidemije"})
    check("matching kind is delivered", 711 in epi, repr(list(epi)))
    check("unfiltered location still matches", 712 in epi, repr(list(epi)))
    check("other kind is skipped", 713 not in epi, repr(list(epi)))

    fire = S.match_subscribers(46.0569, 14.5058, {"Požar, eksplozija"})
    check("unmatched kind skips the filtered location", 711 not in fire, repr(list(fire)))
    check("unmatched kind still reaches the unfiltered location", 712 in fire, repr(list(fire)))

    both = S.match_subscribers(46.0569, 14.5058, {"Epidemije", "Gore"})
    check("several kinds reach several locations", 711 in both and 713 in both, repr(list(both)))

    everything = S.match_subscribers(46.0569, 14.5058)
    check("no kinds given reaches every location",
          {711, 712, 713} <= set(everything), repr(list(everything)))
    check("empty kind set reaches every location",
          {711, 712, 713} <= set(S.match_subscribers(46.0569, 14.5058, set())),
          repr(list(S.match_subscribers(46.0569, 14.5058, set()))))
    check("distance still applies with a filter",
          711 not in S.match_subscribers(46.5547, 15.6459, {"Epidemije"}))


def test_delivery():
    for chat in (721, 722):
        S.remove_all_subscriptions(chat)
        S.forget_delivery(chat, INCIDENT["id"]) if hasattr(S, "forget_delivery") else None
    S.add_subscription(721, "Ljubljana", 46.0569, 14.5058, 10, topics=["Epidemije"])
    S.add_subscription(722, "Ljubljana vse", 46.0569, 14.5058, 10)

    real_map = B.create_static_map_image

    def fake_map(lat, lon, filename="incident_map.png", **kw):
        with open(filename, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    B.create_static_map_image = fake_map
    try:
        bot = FakeBot()
        asyncio.get_event_loop().run_until_complete(
            B.notify_radius_subscribers(bot, INCIDENT, DETAILS_FIRE,
                                        {"Požar, eksplozija"}))
        sent = [photo["chat_id"] for photo in bot.photos]
        check("kind filter keeps the wrong-kind chat silent", 721 not in sent, repr(sent))
        check("kind filter does not block the unfiltered chat", 722 in sent, repr(sent))
        check("skipped chat is not marked delivered", not S.already_delivered(721, INCIDENT["id"]))
        check("delivered chat is recorded", S.already_delivered(722, INCIDENT["id"]))
    finally:
        B.create_static_map_image = real_map
        if os.path.exists("subscriber_map.png"):
            os.remove("subscriber_map.png")

    S.record_delivery(721, "777777")   # simulate a bounce back in time
    S.record_delivery(721, INCIDENT["id"])


def test_incident_kinds():
    kinds = {k for k, _label in S.TOPIC_CHOICES}
    check("an incident type is offered as a kind",
          "Požar, eksplozija" in kinds and "Epidemije" in kinds)
    # the keyword topics the bot derives from the incident text must be offerable
    for topic in B.match_keywords_in_dogodek("Jedrska nesreča v Krškem"):
        check("keyword topic %r is offered" % topic, topic in kinds, repr(topic))


def main():
    for fn in (test_vocabulary, test_encode_decode, test_store, test_matching,
               test_delivery, test_incident_kinds):
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
