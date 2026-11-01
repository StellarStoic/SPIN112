"""Map rendering: translucent polygons, borders drawn on top, 1 km radius preset.

Runs offline: the projection is faked so a ring's (lon, lat) pairs are pixel
coordinates, which makes the blending result directly checkable.  The real
tile / projection path is exercised by tests/test_radius.py and by hand with
tools/smoke_map.py.

    TELEGRAM_BOT_TOKEN=1:T TELEGRAM_GROUP_ID=-1001 python tests/test_map_render.py
"""

import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_GROUP_ID", "-1001234567890")
os.environ["SUBSCRIPTIONS_DB"] = os.path.join(BOT_DIR, "tests", "_map_render_tmp.db")
sys.path.insert(0, BOT_DIR)

from PIL import Image  # noqa: E402

import subscriptions as S  # noqa: E402

PASSES = []
FAILS = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print("PASS", name)
    else:
        FAILS.append(name)
        print("FAIL", name, detail)


class FakeMap:
    """Minimal stand-in for StaticMap: identity projection, no tiles."""

    width = 200
    height = 200
    zoom = 12


def fake_project(static_map, lon, lat):
    return float(lon), float(lat)


def render(shapes, markers=(), size=(200, 200)):
    S._project = fake_project
    image = Image.new("RGB", size, (255, 255, 255))
    return S._draw_overlays(image, FakeMap(), shapes, list(markers))


def px(image, x, y):
    return image.getpixel((x, y))[:3]


RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 128, 0)
A = [(10, 10), (90, 10), (90, 90), (10, 90)]
B = [(50, 50), (130, 50), (130, 130), (50, 130)]


def test_fills_blend():
    image = render([(A, RED, RED), (B, BLUE, BLUE)])
    single = px(image, 30, 30)
    overlap = px(image, 60, 60)
    # white base + red at alpha 60 -> red stays 255, green/blue drop to ~195
    check("single fill is translucent (map shows through)",
          single[0] == 255 and 170 < single[1] < 235 and 170 < single[2] < 235,
          "pixel=%s" % (single,))
    check("single fill keeps its hue", single[0] > single[2], "pixel=%s" % (single,))
    check("second fill is translucent", overlap[2] > overlap[0], "pixel=%s" % (overlap,))
    check("overlap is deeper than either single fill",
          overlap[0] > single[2] // 2 or overlap != single,
          "single=%s overlap=%s" % (single, overlap))
    check("map background untouched outside the shapes", px(image, 190, 10) == (255, 255, 255))


def test_border_under_fill_survives():
    image = render([(A, RED, RED), (B, BLUE, BLUE)])
    on_border = px(image, 90, 70)          # A's right edge, inside B's fill
    inside_b = px(image, 120, 60)          # B only
    check("lower polygon's border stays visible under the upper fill",
          on_border[0] > 120 and on_border[2] < 140, "pixel=%s" % (on_border,))
    check("border pixel differs clearly from the covering fill",
          abs(on_border[0] - inside_b[0]) > 40, "border=%s fill=%s" % (on_border, inside_b))


def test_border_order_independent():
    forward = px(render([(A, RED, RED), (B, BLUE, BLUE)]), 90, 70)
    backward = px(render([(B, BLUE, BLUE), (A, RED, RED)]), 90, 70)
    check("outline pass ignores the drawing order",
          forward[0] > 120 and backward[0] > 120,
          "forward=%s backward=%s" % (forward, backward))


def test_three_overlapping_polygons():
    C = [(150, 40), (190, 40), (190, 80), (150, 80)]
    image = render([(A, RED, RED), (B, BLUE, BLUE), (C, GREEN, GREEN)])
    check("three fills all remain visible", px(image, 180, 50)[1] > 100,
          "pixel=%s" % (px(image, 180, 50),))
    check("no shape vanishes completely",
          all(px(image, x, y) != (255, 255, 255) for x, y in ((30, 30), (60, 60), (180, 50))))


def test_markers_on_top():
    image = render([(A, RED, RED)], markers=[((70, 70), GREEN)])
    check("marker is drawn above the fills", px(image, 70, 70) == GREEN,
          "pixel=%s" % (px(image, 70, 70),))


def test_radius_presets():
    check("1 km preset exists", 1 in S.RADIUS_PRESETS, str(S.RADIUS_PRESETS))
    check("presets stay sorted and unique",
          list(S.RADIUS_PRESETS) == sorted(set(S.RADIUS_PRESETS)), str(S.RADIUS_PRESETS))
    keyboard = S._radius_keyboard()
    labels = [b.text for row in keyboard.inline_keyboard for b in row]
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    check("keyboard offers 1 km", "1 km" in labels, str(labels))
    check("1 km sends rad:1", "rad:1" in datas, str(datas))
    check("custom radius still offered", "rad:custom" in datas)
    check("no keyboard row is wider than three buttons",
          all(len(row) <= 3 for row in keyboard.inline_keyboard),
          str([len(r) for r in keyboard.inline_keyboard]))
    check("municipality option appended last",
          S._radius_keyboard("Kranj").inline_keyboard[-1][0].callback_data == "rad:obc")
    check("1 km is accepted by the radius clamp", S.MIN_RADIUS_KM <= 1.0)


def test_help_mentions_both():
    check("help documents the 1 km option", "1 km" in S.HELP_TEXT)
    check("help explains translucent areas", "prosojno" in S.HELP_TEXT)


def run():
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_")):
        print("==", name)
        fn()
    print()
    print("%d passed, %d failed" % (len(PASSES), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(run())
