#!/usr/bin/env python3
"""One-off probe: real incident + real map through notify_radius_subscribers."""
import asyncio
import os
import sys

os.environ["SUBSCRIPTIONS_DB"] = "/tmp/probe_subs.db"
BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BOT_DIR)
sys.path.insert(0, BOT_DIR)

import subscriptions as S
import SPIN112 as B


class FakeBot:
    def __init__(self):
        self.photos = []
        self.texts = []

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **kw):
        data = photo.read()
        self.photos.append((chat_id, len(data), caption))
        return True

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.texts.append((chat_id, text))
        return True


def main():
    rss = B.get_rss_feed(B.rss_feed_url)
    incidents = B.parse_rss_feed(rss)
    print("feed incidents:", len(incidents))
    for incident in incidents[:3]:
        detailed = B.get_incident_details(incident["link_suffix"])
        if not detailed or "value" not in detailed:
            continue
        details = detailed["value"]
        lat = details.get("wgsLat") or details.get("wgsLatApprox")
        lon = details.get("wgsLon") or details.get("wgsLonApprox")
        if not (lat and lon):
            continue
        print("using incident", incident["id"], "at", lat, lon)
        S.remove_all_subscriptions(1)
        S.add_subscription(1, "Probe", float(lat), float(lon), 2)

        bot = FakeBot()
        asyncio.get_event_loop().run_until_complete(
            B.notify_radius_subscribers(bot, incident, details))
        print("photos:", [(c, n) for c, n, _ in bot.photos], "texts:", len(bot.texts))
        if bot.photos:
            print("caption chars:", len(bot.photos[0][2] or ""))
            print("caption tail:", (bot.photos[0][2] or "")[-90:].replace("\n", " | "))
        print("recorded:", S.already_delivered(1, incident["id"]))

        bot2 = FakeBot()
        asyncio.get_event_loop().run_until_complete(
            B.notify_radius_subscribers(bot2, incident, details))
        print("second pass photos:", len(bot2.photos), "texts:", len(bot2.texts))
        S.remove_all_subscriptions(1)
        for path in ("subscriber_map.png",):
            if os.path.exists(path):
                print("map file bytes:", os.path.getsize(path))
                os.remove(path)
        return 0
    print("no incident with coordinates found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
