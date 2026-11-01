# AGENTS.md — how this bot works

Instructions for coding agents (and humans) working on the SPIN112 Telegram bot.
Read this before changing anything; it records the parts that are easy to break.

## What it is

A Telegram bot that watches the Slovenian SOS112 public feed (spin3.sos112.si),
posts every new incident into a Telegram group's topics, and — separately — DMs
private chats that follow a place (a radius around a point, or a whole
municipality), optionally filtered to the incident kinds that chat cares about.

Two delivery paths must stay independent:

1. **Group posting** (`SPIN112.py`): "All" topic first, then region topic,
   intervention-type topic and keyword topics. Never make this depend on
   subscriptions.
2. **Private DMs** (`subscriptions.py` via `notify_radius_subscribers`):
   sent only to chats whose followed area contains the incident's coordinates
   *and* whose kind filter matches. Additive only.

## Repo map

| Path | Purpose |
| --- | --- |
| `SPIN112.py` | Feed polling, incident parsing, group topic/keyword posting, per-incident map image, `notify_radius_subscribers()`, `main()` with the job queue and `BotCommand` registration. |
| `subscriptions.py` | Everything about followed locations: SQLite schema, geocoding (Nominatim), municipality index, matching, the `/track`…`/stop` conversation, `/list` overview map rendering, `HELP_TEXT`. |
| `OB.geojson` | Municipality (`občina`) polygons — loaded at start into `OB_INDEX` / `OB_RINGS`. |
| `SR.geojson` | Statistical region polygons, used for the region topic. |
| `tests/` | Standalone scripts, no pytest needed. Each prints `N passed, M failed` and exits non-zero on failure. |
| `tools/smoke_map.py` | Renders a sample `/list` map with overlapping areas to a PNG, no Telegram involved. |
| `subscriptions.db` | Live SQLite DB (not in git; override with `SUBSCRIPTIONS_DB`). |
| `posted_incidents.json` | Group-posting dedupe set (incident IDs only). |

## Runtime and configuration

* Entry point `SPIN112.py`; library `python-telegram-bot` v22 (async, `job_queue`).
* Python 3.9 on the production host — do not use `match`, `X | Y` annotations or
  other 3.10+ syntax in anything that ships there.
* Environment (`.env` next to `SPIN112.py`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_GROUP_ID`.
  Optional: `SUBSCRIPTIONS_DB` (DB path).
* Job queue: `auto_fetch_and_post` every 80 s, `fetch_and_post_vecji_obseg` every
  80 s offset 60 s, `auto_fetch_and_post_nepreverjeni` every 120 s (first-reports topic).
* Local dev interpreter on the Windows box: `C:\Program Files\Python311\python.exe`.

## Incident pipeline (`SPIN112.py`)

```
RSS (ODRSS/false = verified only)  ->  parse_rss_feed
   -> per incident: details from /api/javno/lokacija/<link_suffix>
   -> group: "All" topic, region topic, intervention-type topic, keyword topics
   -> private: notify_radius_subscribers(bot, incident, details, incident_topics)
   -> fetched_incidents.add(id) + write posted_incidents.json
```

* `posted_incidents.json` is read **once** at start into `fetched_incidents`
  (seed-on-first-run): whatever is already in the file is never posted again.
* `incident_topics` = `intervencijaVrstaNaziv` ∪ `match_keywords_in_dogodek(dogodekNaziv)`.
  Those strings are the same keys the group topics use — that is the contract
  between the posting and the filtering side.
* Coordinates come from `wgsLat`/`wgsLon`, falling back to `wgsLatApprox`/`wgsLonApprox`.
  Incidents without coordinates are skipped for DMs (no way to place them).

## Subscriptions (`subscriptions.py`)

SQLite table `subscriptions`:

| column | meaning |
| --- | --- |
| `id`, `chat_id` | row id, private chat |
| `label` | user-given name (≤ 60 chars) — always shown in confirmations |
| `lat`, `lon` | centre |
| `radius_km` | 1–100 (`MIN_RADIUS_KM`/`MAX_RADIUS_KM`); `0` for municipality rows |
| `kind` | `circle` or `obcina` |
| `ob_name` | municipality name (uppercase key into `OB_RINGS`) when `kind='obcina'` |
| `topics` | `|`-separated topic keys, empty = all kinds |
| `geohash` | the cell the circle came from (`u24q4b`), empty for name/coordinate rows |

Table `deliveries(chat_id, incident_id, sent_at)` records what has been DM'd so a
restart cannot send the same incident twice; rows older than
`DELIVERY_RETENTION_DAYS` are pruned by `prune_deliveries()` at startup.

Key functions: `add_subscription`, `list_subscriptions`, `get_subscription`,
`set_topics`, `remove_subscription`, `remove_all_subscriptions`,
`match_subscribers(lat, lon, topics=None)`, `already_delivered` / `record_delivery`,
`render_subscriptions_map(rows, path)`.

`match_subscribers` returns `[(label, distance_km, radius_km)]`: it computes the
haversine distance to each circle and does a point-in-polygon test for each
municipality, then drops rows whose `topics` do not intersect the incident's
kinds (an incident with no known kind is never filtered out). Schema changes go
through the `ALTER TABLE` migration block inside `init()` — the live DB is
migrated in place, so keep migrations additive.

## Conversation (`/track`)

`ConversationHandler` states: `PLACE, PICK, RADIUS, RADIUS_CUSTOM, NAME, TOPICS, TOPIC_PICK`.

```
/track
  -> place name (Nominatim, either candidate towns/villages or municipalities)
     or coordinates "lat lon [radius]" (also "lat, lon, radius", comma decimals)
     or a geohash "gh:u24q4b" (prefix optional when geocoding finds nothing)
  -> PICK candidate from an inline keyboard
  -> RADIUS preset buttons (1, 5, 10, 25, 50 km) | "✏️ drugo" (1–100 km) | municipality option
     (geohash input skips this step entirely – a cell has no radius)
  -> NAME (mandatory free-text name, or the suggested button)
  -> TOPICS toggle keyboard (empty = all kinds) + "Shrani"
  -> _commit() -> add_subscription()
```

### Geohash locations

A geohash is a **rectangle**, so it is stored as the cell itself: the row gets
`kind = 'geohash'`, `radius_km = 0` and the hash in `geohash`, and
`match_subscribers()` tests the incident with `point_in_box()` instead of
haversine. Nothing in the geohash path asks for or uses a radius.

* `is_geohash()`, `decode_geohash()` (centre + half-extents), `encode_geohash()`.
* `geohash_bbox(gh)` → `(lat_min, lat_max, lon_min, lon_max)` is the watch area;
  `point_in_box(lat, lon, box)` is the whole matching rule; `box_ring(box)` is the
  closed lon/lat ring the map draws; `geohash_cell_size_km(gh)` →
  `(width_km, height_km)` exists only for messages.
* `parse_geohash_input(text, require_prefix=False)` accepts `gh:<hash>`,
  `geohash <hash>`, a bare `<hash>` and reports `bbox` + `size_km` + `explicit`.
  A trailing number (`gh:u24q4b 10`) is accepted so old habits do not break, but
  it is **ignored** – the cell is the area.
* Routing: `_start_from_text()` tries a **prefixed** geohash first, then
  coordinates, then geocoding. A bare hash is only tried after geocoding returns
  nothing, so places whose names look hash-ish still win.
* `GEOHASH_WATCH_MIN_LEN = 4` (~39 km cell). Shorter hashes are refused with the
  cell size in km and a hint suggesting `encode_geohash(lat, lon, 4)`; the centre
  must be inside the Slovenia bounding box.
* `_use_geohash()` stores `bbox` in `pending` and returns `'name'` straight away
  (or `'invalid'`), so no radius keyboard is ever built for a geohash.

All handlers are private-chat only (`_private_only`, `_refuse_group`), the state
lives in `context.user_data["pending"]`. `RADIUS_PRESETS` is the single source for
the radius buttons, the help text wording and `tests/test_map_render.py` — only
`_radius_keyboard()` builds the rows (3 per row).

Commands (also registered in Telegram's menu via `register_bot_commands`):
`/track`, `/list`, `/topics`, `/remove`, `/stop`, `/help`. `MAX_SUBSCRIPTIONS = 15`
per chat.

## `/list` map

`render_subscriptions_map()` draws every followed area onto one OpenTopoMap image
(1000×760). Why it looks the way it does:

* Fills are **translucent** (`fill_alpha=60`) and composited one shape at a time,
  so overlapping areas deepen instead of hiding each other and the basemap stays
  readable underneath.
* **All outlines are drawn in a final pass**, after every fill, so the border of
  an area that is covered by another area is still fully visible.
* Shapes are also drawn at 2× and downsampled (`scale=2`) for anti-aliased edges;
  markers are drawn in that same final overlay, on top of the fills.
* Areas are registered with `StaticMap.add_polygon(Polygon(ring, None, None))`
  only so `StaticMap` computes the same zoom/centre it would for visible
  polygons; the actual drawing is ours (`_project` + `_draw_overlays`).
  `_project` uses `staticmap.staticmap._lon_to_x/_lat_to_y` — if a future
  staticmap version removes them, `render_subscriptions_map` falls back to
  `_render_plain_map()` (flat polygons, old behaviour) and logs a warning.
* Colours/markers come from `MAP_COLORS`/`MAP_EMOJI` by row index and must stay in
  sync with `_list_caption()`, which explains the legend.

## Tests

No pytest, no network, no Telegram — every test fakes what it needs and uses a
temp DB (`SUBSCRIPTIONS_DB`):

```sh
TELEGRAM_BOT_TOKEN=123:TEST TELEGRAM_GROUP_ID=-1001234567890 \
  /c/Program\ Files/Python311/python.exe tests/test_radius.py
```

| File | Covers |
| --- | --- |
| `test_radius.py` | parsing, schema, matching, municipalities, delivery dedupe |
| `test_topic.py` | topic encode/decode/describe, `match_subscribers` filtering |
| `test_topic_flow.py` | the real handlers driven by fake Update/Context objects |
| `test_nepreverjeni.py` | first-reports feed helpers |
| `test_map_render.py` | polygon transparency, border order, marker order, 1 km preset |
| `test_geohash.py` | decode/encode round trips, parsing, cell radius, geohash DM flow, schema migration |

Run every file before deploying; they must all end in `0 failed`.

## Deploy

The bot runs on a small Linux host under systemd (`spin112.service`,
`WorkingDirectory=~/bots/SPIN112`, `Restart=always`, logs to journald).

```sh
# from the repo root, after tests pass
scp subscriptions.py SPIN112.py rpi0@<host>:~/bots/SPIN112/   # plus tests/ if they changed
ssh rpi0@<host> 'cd ~/bots/SPIN112 && cp -n subscriptions.py subscriptions.py.bak-<tag> && python3 -m py_compile subscriptions.py SPIN112.py'
ssh rpi0@<host> 'sudo -n systemctl restart spin112'
ssh rpi0@<host> 'systemctl show spin112 -p MainPID -p ExecMainStartTimestamp'   # proves the restart
ssh rpi0@<host> 'journalctl -u spin112 -n 40 -p warning'
```

A plain `systemctl restart` over non-interactive SSH can silently do nothing —
always use `sudo -n` and confirm `MainPID`/`ExecMainStartTimestamp` changed.
`Restart=always` means a syntax error shows up as a crash loop in the journal, not
as a failed restart.

## Rules

* **Never repost incidents.** `posted_incidents.json` is the source of truth for
  what the group has seen; do not clear it, do not backfill the group or any topic,
  and do not "re-run" a dry run that already posted.
* Group/topic/keyword posting behaviour is not to be changed while working on
  subscriptions, and vice versa.
* No secrets in the repo, in logs, or in messages: the token lives in `.env` only.
* Keep identifying infrastructure details (host names, addresses, LAN IPs,
  account names) out of anything that gets pushed or posted.
* Text users see is Slovenian; keep new strings in Slovenian and consistent with
  the existing tone (informal `vi`, short sentences).
