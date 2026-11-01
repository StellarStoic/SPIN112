"""Every watch kind gets its own shape in /list, in its colour from the map.

Municipalities are drawn as a border on the map, geohashes as a cell rectangle
and radius watches as a circle, so the list uses a heart, a square and a circle.
The colour of row N is the colour of area N on the map, which is what makes the
list and the picture line up.
"""

import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_GROUP_ID", "-1001234567890")
os.environ["SUBSCRIPTIONS_DB"] = os.path.join(BOT_DIR, "tests", "_listemoji_tmp.db")

import subscriptions as S  # noqa: E402

PASSES = []
FAILS = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"PASS {name}")
    else:
        FAILS.append(name)
        print(f"FAIL {name} {detail}".rstrip())


def _row(**over):
    row = {
        "id": 1,
        "label": "Test",
        "lat": 46.2397,
        "lon": 14.3567,
        "kind": "circle",
        "radius_km": 5.0,
        "ob_name": None,
        "geohash": None,
        "topics": None,
    }
    row.update(over)
    return row


def test_radius_uses_a_circle():
    row = _row()
    assert S.kind_shape(row) == "circle"
    assert S.kind_emoji(row) == "\U0001F534"            # red, index 0
    assert S.kind_emoji(row, 2) == "\U0001F7E1"        # yellow, index 2


def test_geohash_uses_a_square():
    row = _row(kind="geohash", geohash="u24q4b", radius_km=0.0)
    assert S.kind_shape(row) == "square"
    assert S.kind_emoji(row) == "\U0001F7E5"           # red square
    assert S.kind_emoji(row, 4) == "\U0001F7E6"        # blue square


def test_municipality_uses_a_heart():
    row = _row(kind="obcina", ob_name="Ptuj", radius_km=0.0)
    assert S.kind_shape(row) == "heart"
    assert S.kind_emoji(row) == "\u2764\ufe0f"          # red heart
    assert S.kind_emoji(row, 4) == "\U0001F499"        # blue heart


def test_a_row_with_a_geohash_is_a_square_even_if_kind_is_wrong():
    row = _row(kind="circle", geohash="u24q4b", radius_km=0.0)
    assert S.kind_shape(row) == "square"


def test_unknown_kind_falls_back_to_a_circle():
    row = _row(kind=None)
    assert S.kind_shape(row) == "circle"
    assert S.kind_emoji(row) == "\U0001F534"


def test_index_wraps_around_the_palette():
    row = _row()
    assert S.kind_emoji(row, len(S.MAP_COLORS)) == S.kind_emoji(row, 0)


def test_every_palette_colour_has_a_circle_a_square_and_a_heart():
    for shape, glyphs in S.SHAPE_EMOJI.items():
        assert len(glyphs) == len(S.MAP_COLORS), (shape, glyphs)
        assert len(set(glyphs)) == len(glyphs), (shape, glyphs)


def test_list_line_colours_match_the_map_order():
    rows = [
        _row(id=1, label="Dom", kind="circle", radius_km=5.0),
        _row(id=2, label="Celica", kind="geohash", geohash="u24q4b", radius_km=0.0),
        _row(id=3, label="Obcina", kind="obcina", ob_name="Ptuj", radius_km=0.0),
        _row(id=4, label="Drugi", kind="circle", radius_km=1.0),
    ]
    caption = S._list_caption(rows)
    assert "\U0001F534 <code>1</code> \u00b7 <b>Dom</b>" in caption, caption
    assert "\U0001F7E7 <code>2</code> \u00b7 <b>Celica</b> \u00b7 celica u24q4b" in caption, caption
    assert "\U0001F49B <code>3</code> \u00b7 <b>Obcina</b> \u00b7 cela ob\u010dina Ptuj" in caption, caption
    assert "\U0001F7E2 <code>4</code> \u00b7 <b>Drugi</b>" in caption, caption


def test_each_row_keeps_its_own_shape_with_its_colour():
    rows = [
        _row(id=1, label="A", kind="obcina", ob_name="Ptuj", radius_km=0.0),
        _row(id=2, label="B", kind="geohash", geohash="u24q4b", radius_km=0.0),
        _row(id=3, label="C", kind="circle", radius_km=5.0),
    ]
    body = S._list_caption(rows).split("\n\n")[0]      # rows only, no legend
    assert body.count("\u2764") == 1, body            # heart, municipality
    assert body.count("\U0001F7E7") == 1, body        # orange square, geohash
    assert body.count("\U0001F7E1") == 1, body        # yellow circle, radius


def test_legend_explains_colour_and_shape():
    legend = S._list_caption([_row()]).split("Barva se ujema")[1]
    assert "zemljevidu" in legend, legend
    assert "ob\u010dina" in legend or "obcina" in legend, legend


def test_remove_confirmation_carries_the_rows_own_marker():
    row = _row(kind="geohash", geohash="u24q4b", radius_km=0.0)
    question = S._remove_question(row, 4)
    assert "\U0001F7E6 celica u24q4b" in question, question


def _cleanup():
    for suffix in ("", "-journal"):
        try:
            os.remove(os.path.join(BOT_DIR, "tests", "_listemoji_tmp.db") + suffix)
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
