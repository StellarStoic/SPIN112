"""Every watch kind gets its own shape in /list: triangle, rectangle, circle.

Municipalities are drawn as a border on the map, geohashes as a cell rectangle
and radius watches as a circle - the list must use the same three shapes so the
two views match.
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
    assert S.kind_emoji(_row()) == "🔵"


def test_geohash_uses_a_rectangle():
    row = _row(kind="geohash", geohash="u24q4b", radius_km=0.0)
    assert S.kind_emoji(row) == "🔲"


def test_municipality_uses_a_triangle():
    row = _row(kind="obcina", ob_name="Ptuj", radius_km=0.0)
    assert S.kind_emoji(row) == "🔺"


def test_unknown_kind_falls_back_to_a_circle():
    assert S.kind_emoji(_row(kind=None)) == "🔵"


def test_list_line_carries_the_shape_before_the_scope():
    rows = [
        _row(id=1, label="Dom", kind="circle", radius_km=5.0),
        _row(id=2, label="Celica", kind="geohash", geohash="u24q4b", radius_km=0.0),
        _row(id=3, label="Obcina", kind="obcina", ob_name="Ptuj", radius_km=0.0),
    ]
    caption = S._list_caption(rows)
    assert "🔵 <code>1</code> · <b>Dom</b>" in caption, caption
    assert "🔲 <code>2</code> · <b>Celica</b> · celica u24q4b" in caption, caption
    assert "🔺 <code>3</code> · <b>Obcina</b> · cela občina Ptuj" in caption, caption
    assert "🔴" not in caption and "🟢" not in caption, caption


def test_legend_names_all_three_shapes():
    legend = S._list_caption([_row()]).split("Oblika pove")[1]
    for shape in ("🔺", "🔲", "🔵"):
        assert shape in legend, legend


def test_remove_confirmation_carries_the_shape():
    question = S._remove_question(_row(kind="geohash", geohash="u24q4b", radius_km=0.0))
    assert "🔲 celica u24q4b" in question, question


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
