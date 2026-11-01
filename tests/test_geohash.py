#!/usr/bin/env python3
"""Tests for geohash watching locations in SPIN112.

Run from the bot directory:

    "/c/Program Files/Python311/python.exe" tests/test_geohash.py

Uses a throwaway SQLite file and fake Telegram objects, so nothing touches the
live database or the network.
"""
import asyncio
import os
import re
import sys
import tempfile

DB_TMP = os.path.join(tempfile.mkdtemp(prefix="spin_gh_"), "test_gh.db")
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


# --- fake Telegram plumbing ------------------------------------------------

class FakeMessage:
    def __init__(self, sink):
        self.sink = sink

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kw):
        self.sink.append({"text": text, "keyboard": reply_markup, "parse_mode": parse_mode})
        return True


class FakeUser:
    id = 424242
    first_name = "Test"
    username = "test"


class FakeUpdate:
    def __init__(self):
        self.sent = []
        self.effective_message = FakeMessage(self.sent)
        self.callback_query = None
        self.effective_user = FakeUser()
        self.effective_chat = type("C", (), {"id": 424242, "type": "private"})()


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.chat_data = {}
        self.bot_data = {}


def call(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def keyboard_labels(markup):
    labels = []
    for row in getattr(markup, "inline_keyboard", []) or []:
        for button in row:
            labels.append((button.text, button.callback_data))
    return labels


# --- pure geohash helpers --------------------------------------------------

def test_alphabet_and_recognition():
    check("alphabet has 32 chars", len(S.GEOHASH_ALPHABET) == 32, S.GEOHASH_ALPHABET)
    check("alphabet omits a, i, l, o",
          not set("ailo") & set(S.GEOHASH_ALPHABET), S.GEOHASH_ALPHABET)
    check("is_geohash accepts u24q4b", S.is_geohash("u24q4b"))
    check("is_geohash accepts uppercase", S.is_geohash("U24Q4B"))
    check("is_geohash rejects a place name", not S.is_geohash("Ljubljana"))
    check("is_geohash rejects forbidden letters", not S.is_geohash("u2i4qb"))
    check("is_geohash rejects punctuation", not S.is_geohash("u24q-b"))
    check("is_geohash rejects too long", not S.is_geohash("u24q4bq1u24q4"))
    check("is_geohash honours min_len", not S.is_geohash("u24", min_len=4))


def test_decode_reference_points():
    # geohash.org reference sample
    lat, lon = S.decode_geohash("u4pruydqqvj")[:2]
    check("decode u4pruydqqvj lat ~57.64911", abs(lat - 57.64911) < 0.001, "got %.5f" % lat)
    check("decode u4pruydqqvj lon ~10.40744", abs(lon - 10.40744) < 0.001, "got %.5f" % lon)

    lj = S.decode_geohash("u24q")
    check("decode u24q centre near Ljubljana",
          45.9 < lj[0] < 46.3 and 14.3 < lj[1] < 14.9, "got %s" % (lj[:2],))
    check("decode returns half-extents", len(lj) == 4 and lj[2] > 0 and lj[3] > 0, repr(lj))

    check("decode rejects bad input", S.decode_geohash("u24q!") is None)
    check("decode rejects empty", S.decode_geohash("") is None)
    check("decode rejects over-long input", S.decode_geohash("u" * 13) is None)


def test_encode_roundtrip():
    lat, lon = 46.0569, 14.5058
    check("encode 6 chars of Ljubljana", S.encode_geohash(lat, lon, 6) == "u24q40",
          S.encode_geohash(lat, lon, 6))
    check("encode 4 chars of Ljubljana", S.encode_geohash(lat, lon, 4) == "u24q",
          S.encode_geohash(lat, lon, 4))
    for precision in (4, 5, 6, 7, 9):
        token = S.encode_geohash(lat, lon, precision)
        check("encode length %d" % precision, len(token) == precision, token)
        check("encoded token is valid geohash %d" % precision, S.is_geohash(token), token)
        back = S.decode_geohash(token)
        # centre must lie inside the cell the point belongs to
        check("roundtrip keeps point inside cell %d" % precision,
              abs(back[0] - lat) <= back[2] + 1e-9 and abs(back[1] - lon) <= back[3] + 1e-9,
              "back=%s lat=%s lon=%s" % (back, lat, lon))


def test_cell_size_shrinks_with_length():
    sizes = [S.geohash_cell_size_km("u24q4bq1"[:n]) for n in range(3, 8)]
    check("cell size shrinks as the hash grows",
          all(sizes[i][0] > sizes[i + 1][0] and sizes[i][1] > sizes[i + 1][1]
              for i in range(len(sizes) - 1)), repr(sizes))
    check("3-char cell is huge", sizes[0][0] > 80, repr(sizes))
    check("6-char cell is about a kilometre wide", 0.5 < sizes[3][0] < 2.0, repr(sizes))
    check("6-char cell is narrower than a degree", sizes[3][1] < 2.0, repr(sizes))


def test_bbox_is_a_rectangle():
    gh = "u24q4b"
    box = S.geohash_bbox(gh)
    lat_min, lat_max, lon_min, lon_max = box
    check("bbox has four increasing bounds",
          lat_min < lat_max and lon_min < lon_max, repr(box))
    centre = S.decode_geohash(gh)
    check("bbox agrees with the decoded cell",
          abs(lat_min - (centre[0] - centre[2])) < 1e-9
          and abs(lat_max - (centre[0] + centre[2])) < 1e-9
          and abs(lon_min - (centre[1] - centre[3])) < 1e-9
          and abs(lon_max - (centre[1] + centre[3])) < 1e-9, repr((box, centre)))
    check("cell centre is inside its own box", S.point_in_box(centre[0], centre[1], box), repr(box))
    # a point just outside the east edge must be outside, however close it is
    check("a hair east of the cell is outside",
          not S.point_in_box(centre[0], lon_max + 1e-6, box), repr(box))
    check("a hair north of the cell is outside",
          not S.point_in_box(lat_max + 1e-6, centre[1], box), repr(box))
    ring = S.box_ring(box)
    check("box ring is closed", ring[0] == ring[-1], repr(ring))
    check("box ring is a lon/lat rectangle",
          sorted(set(round(p[0], 6) for p in ring)) == sorted({round(lon_min, 6), round(lon_max, 6)})
          and sorted(set(round(p[1], 6) for p in ring)) == sorted({round(lat_min, 6), round(lat_max, 6)}),
          repr(ring))


def test_parse_input():
    parsed = S.parse_geohash_input("gh:u24q4b")
    check("parse accepts gh: prefix", parsed and parsed["geohash"] == "u24q4b", repr(parsed))
    check("parse records the centre", parsed and abs(parsed["lat"] - 46.05) < 0.5, repr(parsed))

    parsed = S.parse_geohash_input("geohash: u24q4b 12")
    check("parse still tolerates a trailing number",
          parsed and parsed["geohash"] == "u24q4b", repr(parsed))
    check("parse exposes the cell rectangle",
          parsed and len(parsed.get("bbox") or ()) == 4, repr(parsed))
    check("parse exposes the cell size in km",
          parsed and parsed["size_km"][0] > 0, repr(parsed))
    check("parse no longer reports a cell radius",
          parsed and "cell_radius_km" not in parsed, repr(parsed))

    check("prefix regex strips the marker",
          S._GEOHASH_PREFIX_RE.sub("", "Geohash: u24q4b") == "u24q4b")

    parsed = S.parse_geohash_input("u24q4b")
    check("bare hash parses without prefix", parsed and parsed["geohash"] == "u24q4b", repr(parsed))
    check("bare hash is marked implicit", parsed and not parsed["explicit"], repr(parsed))
    check("prefix hash is marked explicit",
          S.parse_geohash_input("gh:u24q4b")["explicit"] is True)

    check("require_prefix rejects bare hash", S.parse_geohash_input("u24q4b", require_prefix=True) is None)
    check("require_prefix keeps gh: hash",
          S.parse_geohash_input("gh:u24q4b", require_prefix=True)["geohash"] == "u24q4b")
    check("parse rejects a place name", S.parse_geohash_input("Ljubljana", require_prefix=True) is None)
    check("parse rejects bad radius", S.parse_geohash_input("gh:u24q4b abc", require_prefix=True) is None)


def test_conversation_paths():
    update, context = FakeUpdate(), FakeContext()

    parsed = S.parse_geohash_input("gh:u24q4b")
    state = call(S._use_geohash(update, context, parsed))
    check("geohash skips the radius and asks for a name", state == "name", repr(state))
    cell_msg = update.sent[-2]["text"]
    name_msg = update.sent[-1]["text"]
    check("cell question mentions the rectangle", "pravokotnik" in cell_msg, cell_msg)
    check("cell question quotes the bounds", "–" in cell_msg, cell_msg)
    check("cell question offers no radius", "polmer" not in cell_msg.lower(), cell_msg)
    check("name prompt says there is no radius", "brez polmera" in name_msg, name_msg)
    callbacks = []
    for msg in update.sent:
        keyboard = msg.get("keyboard")
        if keyboard:
            callbacks.extend(data for _, data in keyboard_labels(keyboard))
    check("no radius keyboard is sent for a geohash",
          not [c for c in callbacks if c.startswith("rad:")], repr(callbacks))
    check("pending keeps the geohash", context.user_data["pending"].get("geohash") == "u24q4b",
          repr(context.user_data["pending"]))
    check("pending stores the cell rectangle",
          len(context.user_data["pending"].get("bbox") or ()) == 4,
          repr(context.user_data["pending"]))
    check("pending stores no radius",
          not context.user_data["pending"].get("radius_km"),
          repr(context.user_data["pending"]))

    update, context = FakeUpdate(), FakeContext()
    state = call(S._use_geohash(update, context, S.parse_geohash_input("gh:u24q4b 7")))
    check("a trailing radius is ignored", state == "name", repr(state))
    check("ignored radius is not stored", not context.user_data["pending"].get("radius_km"),
          repr(context.user_data["pending"]))

    update, context = FakeUpdate(), FakeContext()
    state = call(S._use_geohash(update, context, S.parse_geohash_input("gh:u24")))
    check("too coarse geohash is refused", state == "invalid", repr(state))
    check("refusal suggests a longer hash", "znakov" in update.sent[-1]["text"],
          update.sent[-1]["text"])
    check("refusal quotes the cell size in km", "km" in update.sent[-1]["text"],
          update.sent[-1]["text"])

    update, context = FakeUpdate(), FakeContext()
    state = call(S._use_geohash(update, context, S.parse_geohash_input("gh:9q8yyk")))
    check("geohash outside Slovenia is refused", state == "invalid", repr(state))
    check("refusal names the country", "Slovenij" in update.sent[-1]["text"],
          update.sent[-1]["text"])


def test_storage_and_matching():
    S.remove_all_subscriptions(500)
    S.remove_all_subscriptions(501)

    gh = S.encode_geohash(46.0569, 14.5058, 6)
    lat, lon, lat_err, lon_err = S.decode_geohash(gh)
    box = S.geohash_bbox(gh)
    S.add_subscription(500, "Geohash %s" % gh, lat, lon, 0.0, "geohash", None, None, gh)

    rows = S.list_subscriptions(500)
    check("geohash stored with the row", rows and rows[0]["geohash"] == gh, repr(rows))
    check("geohash row keeps its kind", rows and rows[0]["kind"] == "geohash", repr(rows))
    check("geohash row stores no radius", rows and rows[0]["radius_km"] == 0.0, repr(rows))
    check("describe shows the cell", S.describe_subscription(rows[0]) == "celica %s" % gh,
          S.describe_subscription(rows[0]))

    hits = S.match_subscribers(46.0569, 14.5058)
    check("incident inside the cell matches", 500 in hits, repr(list(hits)))

    inside_corner = S.match_subscribers(box[0] + 1e-6, box[2] + 1e-6)
    check("incident near the cell corner still matches", 500 in inside_corner,
          repr(list(inside_corner)))
    over_the_line = S.match_subscribers(lat, box[3] + 0.01)
    check("incident just outside the cell does not match", 500 not in over_the_line,
          repr(list(over_the_line)))
    check("that outside point is still close in km",
          S.haversine_km(lat, box[3] + 0.01, lat, lon) < 5.0,
          repr(S.haversine_km(lat, box[3] + 0.01, lat, lon)))

    far = S.match_subscribers(46.5547, 15.6459)
    check("incident in Maribor does not match", 500 not in far, repr(list(far)))

    S.add_subscription(501, "Ghost", 46.0569, 14.5058, 5)
    rows = S.list_subscriptions(501)
    check("plain coordinate row has no geohash", rows and rows[0]["geohash"] is None, repr(rows))
    check("plain row describes as a radius",
          rows and re.match(r"^5 km$", S.describe_subscription(rows[0])),
          rows and S.describe_subscription(rows[0]))

    # topic filter still applies to geohash rows
    S.add_subscription(500, "Geohash %s" % gh, lat, lon, 0.0, "geohash", None,
                       ["Epidemije"], gh)
    only_fire = S.match_subscribers(46.0569, 14.5058, topics=["Požar, eksplozija"])
    check("topic filter silences a geohash row", 500 not in only_fire, repr(list(only_fire)))
    only_epi = S.match_subscribers(46.0569, 14.5058, topics=["Epidemije"])
    check("matching topic reaches the geohash row", 500 in only_epi, repr(list(only_epi)))

    S.remove_all_subscriptions(500)
    S.remove_all_subscriptions(501)


def test_migration_from_old_schema():
    legacy = os.path.join(tempfile.mkdtemp(prefix="spin_gh_legacy_"), "legacy.db")
    import sqlite3
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE subscriptions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
        "label TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, "
        "radius_km REAL NOT NULL, created_at TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL DEFAULT 'circle', "
        "ob_name TEXT)"
    )
    conn.execute(
        "INSERT INTO subscriptions (chat_id, label, lat, lon, radius_km, created_at) "
        "VALUES (600, 'Old', 46.0, 14.0, 10, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    S.init(db_path=legacy)
    rows = S.list_subscriptions(600)
    check("legacy row survives the migration", len(rows) == 1, repr(rows))
    check("legacy row got a NULL geohash", rows and rows[0]["geohash"] is None, repr(rows))
    S.add_subscription(600, "New", 46.05, 14.05, 3, "circle", None, None, "u24q4b")
    rows = S.list_subscriptions(600)
    newer = [row for row in rows if row["label"] == "New"]
    check("geohash can be written after migrating", newer and newer[0]["geohash"] == "u24q4b",
          repr(rows))


def test_map_draws_the_cell_as_a_rectangle():
    """The plain-map fallback must draw box_ring(), never a circle ring."""
    import types

    captured = {"polygons": [], "lines": [], "markers": []}

    class FakePolygon(object):
        def __init__(self, ring, fill, outline):
            captured["polygons"].append(list(ring))

    class FakeLine(object):
        def __init__(self, ring, color, width):
            captured["lines"].append(list(ring))

    class FakeMarker(object):
        def __init__(self, point, color, radius):
            captured["markers"].append(point)

    class FakeImage(object):
        def save(self, path):
            with open(path, "wb") as handle:
                handle.write(b"png")

    class FakeStaticMap(object):
        def __init__(self, *args, **kwargs):
            self.width, self.height = 800, 600

        def add_polygon(self, polygon):
            pass

        def add_line(self, line):
            pass

        def add_marker(self, marker):
            pass

        def render(self):
            return FakeImage()

    fake_module = types.ModuleType("staticmap")
    fake_module.CircleMarker = FakeMarker
    fake_module.Line = FakeLine
    fake_module.Polygon = FakePolygon
    fake_module.StaticMap = FakeStaticMap

    gh = S.encode_geohash(46.0569, 14.5058, 6)
    box = S.geohash_bbox(gh)
    row = {"kind": "geohash", "geohash": gh, "lat": 46.0569, "lon": 14.5058,
           "radius_km": 0.0, "ob_name": None}

    saved = sys.modules.get("staticmap")
    sys.modules["staticmap"] = fake_module
    try:
        path = os.path.join(os.path.dirname(DB_TMP), "map_gh.png")
        S._render_plain_map([row], path, 800, 600)
    finally:
        if saved is None:
            sys.modules.pop("staticmap", None)
        else:
            sys.modules["staticmap"] = saved

    check("map drew one polygon for the cell", len(captured["polygons"]) == 1,
          repr(captured["polygons"]))
    ring = captured["polygons"][0] if captured["polygons"] else []
    check("map polygon is the closed box ring", ring == S.box_ring(box), repr(ring))
    check("map polygon has five points", len(ring) == 5, repr(len(ring)))
    check("map polygon is closed", ring and ring[0] == ring[-1], repr(ring))
    check("map outline follows the same ring",
          captured["lines"] and captured["lines"][0] == S.box_ring(box),
          repr(captured["lines"]))
    check("marker sits on the cell centre", captured["markers"] == [(14.5058, 46.0569)],
          repr(captured["markers"]))
    check("no centre-radius circle was drawn",
          abs(ring[1][0] - ring[0][0]) > 1e-6 and abs(ring[2][1] - ring[1][1]) > 1e-6,
          repr(ring))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    total = PASSES + len(FAILS)
    print("\n%d/%d checks passed" % (PASSES, total))
    if FAILS:
        print("FAILED: " + ", ".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
