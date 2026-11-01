"""Render a sample overview map with overlapping followed areas.

Use it to eyeball the /list map without touching Telegram or the live database:

    python tools/smoke_map.py /tmp/spin_map.png

Needs OB.geojson next to this script's parent directory and a network
connection for the basemap tiles.
"""

import json
import os
import sys
import tempfile

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BOT_DIR)
os.environ.setdefault(
    "SUBSCRIPTIONS_DB",
    os.path.join(tempfile.gettempdir(), "spin112_smoke_map.db"),
)

import subscriptions as S  # noqa: E402


def main(out_path):
    with open(os.path.join(BOT_DIR, "OB.geojson"), encoding="utf-8") as fh:
        features = json.load(fh).get("features", [])
    S.init(ob_features=features)
    print("municipality rings:", len(S.OB_RINGS))

    chat = 555
    S.remove_all_subscriptions(chat)
    # A municipality plus a circle inside it and a circle straddling its border:
    # exactly the case where opaque fills used to hide the lower outline.
    S.add_subscription(chat, "Občina Ljubljana", 46.0569, 14.5058, 0,
                       kind="obcina", ob_name="Ljubljana")
    S.add_subscription(chat, "Center", 46.0511, 14.5051, 5)
    S.add_subscription(chat, "Vič", 46.0400, 14.4800, 10)
    S.add_subscription(chat, "Ulica", 46.0560, 14.5100, 1)
    rows = S.list_subscriptions(chat)

    S.render_subscriptions_map(rows, out_path)
    print("map:", out_path, os.path.getsize(out_path), "bytes")
    print(S._list_caption(rows).replace("<", "\n<"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(BOT_DIR, "smoke_map.png"))
