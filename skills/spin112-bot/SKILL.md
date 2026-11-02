---
name: spin112-bot
description: Use when changing the SPIN112 Telegram bot (group posting, followed locations, radius/municipality DMs, /list map) — safe edit, test and deploy loop. Full reference lives in AGENTS.md at the repo root.
---

# SPIN112 bot — working on it

The SOS112 incident bot has two independent delivery paths: group posting into
topics (`SPIN112.py`) and private DMs to chats that follow a place
(`subscriptions.py`). Read `AGENTS.md` first — it documents the schema, the
conversation states, the map renderer and the deploy host.

## Edit loop

1. Read the relevant function, not just the diff area: `render_subscriptions_map`
   and `_draw_overlays` are order-sensitive, `match_subscribers` is called from
   `SPIN112.py` with the group's own topic strings.
2. Change `subscriptions.py` for anything user-facing about following places;
   touch `SPIN112.py` only where it calls into it (`notify_radius_subscribers`,
   `BotCommand` list, job queue).
3. Keep new user-visible strings Slovenian and consistent with `HELP_TEXT`. The
   Slovenian word for radius is **radij** (`radija`, `radijem`, `radiji`), never
   `polmer`; help links to `https://en.wikipedia.org/wiki/Geohash`, not
   `geohash.org`.
4. Run every test file (they are plain scripts, no pytest):

   ```sh
   for t in test_radius test_topic test_topic_flow test_nepreverjeni test_map_render test_geohash; do
     TELEGRAM_BOT_TOKEN=123:TEST TELEGRAM_GROUP_ID=-1001234567890 \
       "/c/Program Files/Python311/python.exe" tests/$t.py || echo "FAILED: $t"
   done
   ```

   Production runs Python 3.9 — no `match`, no `X | Y` annotations.

5. For map changes, look at the picture as well as the tests:

   ```sh
   TELEGRAM_BOT_TOKEN=123:TEST TELEGRAM_GROUP_ID=-1001234567890 \
     "/c/Program Files/Python311/python.exe" tools/smoke_map.py /tmp/spin_map.png
   ```

6. Deploy and prove it restarted (recipe in `AGENTS.md` → Deploy); a plain
   `systemctl restart` over non-interactive SSH can silently do nothing.

## Pitfalls

* Overlapping areas: fills are translucent **and** all outlines are drawn in a
  final pass. Never move outline drawing inside the per-shape loop — the lower
  polygon's border disappears again.
* `fill_alpha` is per shape and composited sequentially, so stacking N areas
  darkens them; keep it low (60) instead of raising it for visibility.
* `staticmap` has no alpha support of its own; alpha comes from our RGBA
  overlay, and `_project` depends on `staticmap.staticmap._lon_to_x/_lat_to_y`.
  If that import breaks, keep the `_render_plain_map` fallback working.
* Radius presets live in `RADIUS_PRESETS` only; `_radius_keyboard` chunks them
  3 per row, `HELP_TEXT` and `README.md` phrase them to the user.
* Topic filter keys are the exact group-topic strings; an empty selection means
  "all kinds", and incidents with an unknown kind are never filtered out.
* `/contact` replies with an inline URL button pointing at `DEV_CONTACT_URL`
  (`https://t.me/stellarstoic`) and is the one command that also answers inside
  groups. The reply text stays anonymous ("razvijalec bota") — do **not** print
  the handle or the URL as text; the account lives only behind the button, which
  is why `tests/test_contact.py` asserts both are absent from the message.
* Plain private messages are relayed to the developer: `cmd_user_message` copies the text (or forwards the photo/voice/document) to `dev_chat_id()` — the `DEV_CHAT_ID` env var, `0` meaning relay off — prefixed with the sender's name, `@username` and id so the developer can answer. Register `build_relay_handlers()` with `group=1`: group 0 already owns commands and every conversation step, so `/track` input is never mistaken for a message. The confirmation to the user names nobody, the handler is a no-op when the sender *is* the developer, and `tests/test_relay.py` covers text, media, HTML escaping, truncation, the send-failure path and the missing-config path.
* Geohash subscriptions are **rectangles, not circles**: the row stores
  `kind = 'geohash'`, `radius_km = 0` and the hash, and `match_subscribers()`
  tests incidents with `point_in_box(lat, lon, geohash_bbox(gh))`; the map draws
  `box_ring()` polygons. Their radius is not asked for and must never be used.
  `GEOHASH_WATCH_MIN_LEN` (4) is where a hash becomes usable; parsing stays
  tolerant (`gh:`/`geohash`/bare hash, comma decimals, a trailing radius token
  that is accepted-but-ignored) and the bare-hash attempt stays *after*
  geocoding so place names still win.
* Never repost incidents: `posted_incidents.json` is authoritative, do not clear
  it or backfill the group.
* Keep host names, addresses and account names out of anything pushed or posted.
