"""Per-user radius subscriptions for SPIN112.

A user opens a private chat with the bot, runs /track, types a city or village
name (or a raw ``lat lon radius`` triple) and picks a radius.  Every new
incident whose coordinates fall inside that circle — or inside a followed
municipality (občina) — is then delivered to that chat as a private message
(with the same map image the group gets).  /list renders all followed areas on
one map so the user can see at a glance what region they cover.

Storage is a small SQLite file (``subscriptions.db`` by default, next to the
script).  Geocoding uses Nominatim, cached in the database; when Nominatim is
unreachable or returns nothing, municipality centroids from ``OB.geojson`` are
used as a fallback.
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import sqlite3
import time
import warnings
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.warnings import PTBUserWarning
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# SUBSCRIPTIONS_DB lets tests and probes point at a throwaway file
DB_PATH = os.environ.get("SUBSCRIPTIONS_DB", "subscriptions.db")
OB_FEATURES = []
OB_INDEX = {}
OB_RINGS = {}
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "SPIN112-bot/1.0 (SOS112 incident bot; +https://github.com/StellarStoic/SPIN112)"
NOMINATIM_MIN_INTERVAL = 1.1  # Nominatim asks for at most 1 request/second
MAX_RADIUS_KM = 100.0
MIN_RADIUS_KM = 1.0
MAX_FORMATTED_MATCHES = 5
DELIVERY_RETENTION_DAYS = 60

_last_nominatim_call = 0.0

# conversation states
PLACE, PICK, RADIUS, RADIUS_CUSTOM, NAME, TOPICS, TOPIC_PICK = range(7)

RADIUS_PRESETS = (1, 5, 10, 25, 50)

# Slovenia bounding box, used to sanity-check hand-typed coordinates
SI_LAT_MIN, SI_LAT_MAX = 45.35, 46.95
SI_LON_MIN, SI_LON_MAX = 13.20, 16.80

# Geohash (base32; the format is described at https://en.wikipedia.org/wiki/Geohash).
# A cell of n characters is roughly: 3 chars ~156 km, 4 ~39 km, 5 ~4.9 km,
# 6 ~1.2 km, 7 ~150 m. Shorter than GEOHASH_WATCH_MIN_LEN is unusable as a
# watch area, so those are refused with a hint to add characters.
GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"
GEOHASH_INDEX = {char: index for index, char in enumerate(GEOHASH_ALPHABET)}
GEOHASH_MIN_LEN = 1
GEOHASH_MAX_LEN = 12
GEOHASH_WATCH_MIN_LEN = 4
_GEOHASH_RE = re.compile(r"^[0-9bcdefghjkmnpqrstuvwxyz]+$")
_GEOHASH_PREFIX_RE = re.compile(r"^(?:gh|geohash)\s*[:\s]\s*", re.IGNORECASE)

MAX_LABEL_LEN = 60

# Slovenia-focused tile styles for the overview map
MAP_STYLE_URLS = {
    "topo": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    "default": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
}
# One colour per position in the list, used both for the area on the map and for
# the marker in /list. Only colours that exist as a coloured circle, square *and*
# heart are used, so the marker can keep its shape and still match the map.
MAP_COLORS = (
    "#e01b24", "#e66100", "#b89400", "#2ec27e",
    "#1c71d8", "#813d9c", "#8a5a2b", "#241f31",
)
# Same order as MAP_COLORS: [red, orange, yellow, green, blue, purple, brown, black].
SHAPE_EMOJI = {
    "heart": ("❤️", "🧡", "💛", "💚", "💙", "💜", "🤎", "🖤"),
    "square": ("🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬛"),
    "circle": ("🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "🟤", "⚫"),
}
# The circle row of SHAPE_EMOJI: the colour of subscription N, whichever shape.
MAP_EMOJI = SHAPE_EMOJI["circle"]
# Shape per watch kind: a heart for a municipality border, a square for a geohash
# cell, a circle for a radius. (No triangle glyph renders on every client.)
KIND_SHAPE = {"obcina": "heart", "geohash": "square", "circle": "circle"}

MAX_SUBSCRIPTIONS = 15

# Incident kinds a subscriber can pick from. The keys are exactly the names
# SPIN112.py uses for its group topics (intervention types and keyword topics),
# so the incident side can pass its own strings straight in.
TOPIC_CHOICES = (
    ("Epidemije", "🦠 Epidemije"),
    ("Jedrska ali radiološka nevarnost", "☢️ Jedrska/radiološka"),
    ("Požar, eksplozija", "🔥 Požar, eksplozija"),
    ("Prometna nesreča", "🚗 Prometna nesreča"),
    ("Onesnaženje, nesreče z nevarnimi snovmi", "☣️ Onesnaženje/hazmat"),
    ("Nevarne snovi", "🧪 Nevarne snovi (besede)"),
    ("Tehnična in druga pomoč", "🔧 Tehnična pomoč"),
    ("Tehnična in druga pomoč - jama", "🕳 Tehnična pomoč – jama"),
    ("Tehnična in druga pomoč - plaz", "⛰ Tehnična pomoč – plaz"),
    ("Najdbe NUS", "💣 Najdbe NUS"),
    ("Motnje, omejitve in prekinitve oskrbe", "💡 Motnje oskrbe"),
    ("Prevoz pitne vode", "🚚 Prevoz pitne vode"),
    ("Lažne in nepotrebne intervencije", "🚨 Lažne intervencije"),
    ("Gore", "🏔 Gore"),
    ("Športne aktivnosti", "🎿 Športne aktivnosti"),
)
TOPIC_KEYS = tuple(key for key, _label in TOPIC_CHOICES)
TOPIC_LABELS = dict(TOPIC_CHOICES)
TOPIC_SEP = "|"
ALL_TOPICS_TEXT = "vse vrste"


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init(db_path=None, ob_features=None):
    """Create the schema and build the offline municipality index."""
    global DB_PATH, OB_FEATURES
    if db_path:
        DB_PATH = db_path
    if ob_features is not None:
        OB_FEATURES = ob_features
        _build_ob_index()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                radius_km REAL NOT NULL,
                created_at TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                kind TEXT NOT NULL DEFAULT 'circle',
                ob_name TEXT,
                topics TEXT,
                geohash TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_subscription
                ON subscriptions(chat_id, label);
            CREATE TABLE IF NOT EXISTS deliveries (
                chat_id INTEGER NOT NULL,
                incident_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, incident_id)
            );
            CREATE TABLE IF NOT EXISTS geocache (
                query TEXT PRIMARY KEY,
                candidates TEXT NOT NULL,
                cached_at TEXT NOT NULL
            );
            """
        )
        # databases created before občina support lack these two columns
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
        if "kind" not in columns:
            conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN kind TEXT NOT NULL DEFAULT 'circle'"
            )
        if "ob_name" not in columns:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN ob_name TEXT")
        # databases created before kind filters lack this one
        if "topics" not in columns:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN topics TEXT")
        # databases created before geohash support lack this one
        if "geohash" not in columns:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN geohash TEXT")
        conn.commit()
    logger.info("Radius subscriptions ready (%s).", DB_PATH)


def _build_ob_index():
    """Map upper-cased municipality name -> rough centroid and boundary rings."""
    for feature in OB_FEATURES:
        try:
            props = feature["properties"]
            name = props.get("OB_UIME") or props.get("OBIME") or ""
            if not name:
                continue
            coords = feature["geometry"]["coordinates"]
            geometry_type = feature["geometry"]["type"]
            ring = coords[0] if geometry_type == "Polygon" else coords[0][0]
            lat_sum = lon_sum = 0.0
            for pair in ring:
                lon_sum += pair[0]
                lat_sum += pair[1]
            key = name.strip().upper()
            OB_INDEX[key] = (lat_sum / len(ring), lon_sum / len(ring))
            OB_RINGS[key] = _geometry_rings(feature["geometry"])
        except Exception as exc:  # malformed geometry must not break startup
            logger.debug("Skipping municipality for geocoding: %s", exc)
    logger.info("Municipality geocoding index holds %d entries.", len(OB_INDEX))


def _geometry_rings(geometry):
    """Every outer ring of a Polygon/MultiPolygon as [(lon, lat), ...]."""
    try:
        coords = geometry["coordinates"]
        if geometry["type"] == "Polygon":
            rings = [coords[0]]
        elif geometry["type"] == "MultiPolygon":
            rings = [polygon[0] for polygon in coords]
        else:
            return []
    except (KeyError, TypeError, IndexError):
        return []
    return [[(float(pair[0]), float(pair[1])) for pair in ring] for ring in rings if ring]


def _find_ob_feature(name):
    """Return the OB.geojson feature whose municipality name matches ``name``."""
    needle = (name or "").strip().upper()
    if not needle:
        return None
    for feature in OB_FEATURES:
        props = feature.get("properties") or {}
        candidate = (props.get("OB_UIME") or props.get("OBIME") or "").strip().upper()
        if candidate and (candidate == needle or candidate == "OBČINA " + needle):
            return feature
    return None


def point_in_ring(lat, lon, ring):
    """Ray-casting point-in-polygon test. ``ring`` is [(lon, lat), ...]."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            with_denominator = (yj - yi) or 1e-12
            if lon < (xj - xi) * (lat - yi) / with_denominator + xi:
                inside = not inside
        j = i
    return inside


def point_in_obcina(lat, lon, ob_name):
    """True when (lat, lon) falls inside the followed municipality."""
    key = (ob_name or "").strip().upper()
    if not key:
        return False
    rings = OB_RINGS.get(key)
    if not rings:
        feature = _find_ob_feature(ob_name)
        rings = _geometry_rings(feature["geometry"]) if feature else []
        if rings:
            OB_RINGS[key] = rings
    return any(point_in_ring(lat, lon, ring) for ring in rings)


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    r = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def add_subscription(chat_id, label, lat, lon, radius_km, kind="circle", ob_name=None,
                     topics=None, geohash=None):
    """Insert or update one location for one chat. Returns the row id.

    ``kind`` is ``'circle'`` (a point plus radius), ``'obcina'`` (a whole
    municipality, matched by its polygon; radius is stored as 0) or
    ``'geohash'`` (the exact cell rectangle of ``geohash``; radius is stored as
    0 and never used, because a geohash cell is a rectangle, not a circle).
    ``topics`` is the optional list of incident kinds this location should
    report; an empty list means every kind.
    """
    kind = kind if kind in ("obcina", "geohash") else "circle"
    if kind in ("obcina", "geohash"):
        radius_km = 0.0
    else:
        radius_km = max(MIN_RADIUS_KM, min(MAX_RADIUS_KM, float(radius_km)))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO subscriptions
                (chat_id, label, lat, lon, radius_km, created_at, enabled, kind, ob_name,
                 topics, geohash)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(chat_id, label) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                radius_km = excluded.radius_km,
                created_at = excluded.created_at,
                enabled = 1,
                kind = excluded.kind,
                ob_name = excluded.ob_name,
                topics = excluded.topics,
                geohash = excluded.geohash
            """,
            (int(chat_id), label, float(lat), float(lon), radius_km, now, kind, ob_name,
             encode_topics(topics), geohash),
        )
        conn.commit()
        row_id = cur.lastrowid
        if not row_id:
            found = conn.execute(
                "SELECT id FROM subscriptions WHERE chat_id = ? AND label = ?",
                (int(chat_id), label),
            ).fetchone()
            row_id = found["id"] if found else None
    logger.info(
        "Subscription saved: chat %s -> %s (%s).", chat_id, label,
        "občina" if kind == "obcina" else (f"celica {geohash}" if kind == "geohash"
                                           else "%.0f km" % radius_km),
    )
    return row_id


_COLUMNS = "id, chat_id, label, lat, lon, radius_km, kind, ob_name, topics, geohash"


def list_subscriptions(chat_id):
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM subscriptions "
            "WHERE chat_id = ? AND enabled = 1 ORDER BY id",
            (int(chat_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_subscription(chat_id, sub_id):
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM subscriptions WHERE id = ? AND chat_id = ?",
            (int(sub_id), int(chat_id)),
        ).fetchone()
    return dict(row) if row else None


def describe_subscription(row):
    """Human readable one-liner, e.g. '10 km', 'cela občina Ptuj' or 'celica u24q4b'."""
    kind = row.get("kind") or "circle"
    geohash = (row.get("geohash") or "").strip()
    if kind == "obcina":
        name = (row.get("ob_name") or "").strip()
        return f"cela občina {name}" if name else "cela občina"
    if kind == "geohash" or geohash:
        return f"celica {geohash}" if geohash else "celica"
    return f"{(row.get('radius_km') or 0):g} km"


def kind_shape(row):
    """Shape family for a row: 'heart', 'square' or 'circle'."""
    if (row.get("geohash") or "").strip():
        return "square"
    return KIND_SHAPE.get(row.get("kind") or "circle", "circle")


def kind_emoji(row, index=0):
    """The row's marker: the colour of its area on the map, in its kind's shape.

    ``index`` is the row's position in the list — the same index the map uses to
    pick the colour — so /list row N and area N on the map are the same colour.
    """
    shapes = SHAPE_EMOJI[kind_shape(row)]
    return shapes[index % len(shapes)]


def encode_topics(keys):
    """Store a kind selection as a '|' separated string; None means every kind."""
    wanted = []
    for key in keys or []:
        if key in TOPIC_LABELS and key not in wanted:
            wanted.append(key)
    if not wanted or len(wanted) == len(TOPIC_KEYS):
        return None
    return TOPIC_SEP.join(wanted)


def decode_topics(value):
    """Turn a stored value back into the list of chosen kind names."""
    if not value:
        return []
    return [part for part in str(value).split(TOPIC_SEP) if part in TOPIC_LABELS]


def describe_topics(row):
    """Short human summary of a subscription's kind filter, e.g. 'vse vrste'."""
    keys = decode_topics(row.get("topics")) if isinstance(row, dict) else []
    if not keys:
        return ALL_TOPICS_TEXT
    if len(keys) <= 2:
        return ", ".join(TOPIC_LABELS[key].split(" ", 1)[-1] for key in keys)
    return f"{len(keys)} vrst"


def set_topics(chat_id, sub_id, topics):
    """Replace the kind filter of one saved subscription ([] = every kind)."""
    value = encode_topics(topics)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE subscriptions SET topics = ? WHERE id = ? AND chat_id = ?",
            (value, int(sub_id), int(chat_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def remove_subscription(chat_id, sub_id):
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE id = ? AND chat_id = ?",
            (int(sub_id), int(chat_id)),
        )
        conn.commit()
        return cur.rowcount > 0


def remove_all_subscriptions(chat_id):
    with _connect() as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (int(chat_id),))
        conn.commit()
        return cur.rowcount


def disable_chat(chat_id):
    """The user blocked the bot: stop trying to deliver to that chat."""
    with _connect() as conn:
        conn.execute("UPDATE subscriptions SET enabled = 0 WHERE chat_id = ?", (int(chat_id),))
        conn.commit()


def match_subscribers(lat, lon, topics=None):
    """Return {chat_id: [(label, distance_km, radius_km), ...]} for one incident.

    Circle subscriptions match by distance; občina subscriptions match by their
    polygon, so an incident anywhere inside the municipality counts; geohash
    subscriptions match by their cell rectangle – the incident only has to fall
    inside the box, because a geohash is a rectangle and has no radius.

    ``topics`` holds the incident's kinds (the same names the group topics use).
    A subscription with its own kind filter only matches when the two overlap;
    with no/empty ``topics`` the incident counts as unclassified and reaches
    every matching location, so a missing category never silences alerts.
    """
    lat, lon = float(lat), float(lon)
    wanted = {key for key in (topics or []) if key in TOPIC_LABELS} or None
    matches = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, label, lat, lon, radius_km, kind, ob_name, topics, geohash "
            "FROM subscriptions WHERE enabled = 1"
        ).fetchall()
    for row in rows:
        if wanted is not None:
            allowed = set(decode_topics(row["topics"]))
            if allowed and not (allowed & wanted):
                continue
        distance = haversine_km(lat, lon, row["lat"], row["lon"])
        kind = row["kind"] or "circle"
        if kind == "obcina":
            if point_in_obcina(lat, lon, row["ob_name"]):
                matches.setdefault(row["chat_id"], []).append(
                    (row["label"], distance, 0.0)
                )
            continue
        if kind == "geohash":
            box = geohash_bbox(row["geohash"])
            if box and point_in_box(lat, lon, box):
                matches.setdefault(row["chat_id"], []).append(
                    (row["label"], distance, 0.0)
                )
            continue
        if distance <= row["radius_km"]:
            matches.setdefault(row["chat_id"], []).append(
                (row["label"], distance, row["radius_km"])
            )
    for chat_id in matches:
        matches[chat_id].sort(key=lambda item: item[1])
    return matches


def already_delivered(chat_id, incident_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM deliveries WHERE chat_id = ? AND incident_id = ?",
            (int(chat_id), str(incident_id)),
        ).fetchone()
    return row is not None


def record_delivery(chat_id, incident_id):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO deliveries (chat_id, incident_id, sent_at) VALUES (?, ?, ?)",
            (int(chat_id), str(incident_id), now),
        )
        conn.commit()


def prune_deliveries():
    cutoff = (datetime.now(timezone.utc).timestamp()) - DELIVERY_RETENTION_DAYS * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("DELETE FROM deliveries WHERE sent_at < ?", (cutoff_iso,))
        conn.commit()


# --------------------------------------------------------------------------- #
# geocoding
# --------------------------------------------------------------------------- #

def _cache_get(query):
    with _connect() as conn:
        row = conn.execute(
            "SELECT candidates FROM geocache WHERE query = ?", (query.lower(),)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["candidates"])
    except json.JSONDecodeError:
        return None


def _cache_put(query, candidates):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocache (query, candidates, cached_at) VALUES (?, ?, ?)",
            (query.lower(), json.dumps(candidates, ensure_ascii=False), now),
        )
        conn.commit()


def _shorten(display_name):
    parts = [part.strip() for part in display_name.split(",") if part.strip()]
    return ", ".join(parts[:3])


def geocode_nominatim(query):
    """Ask Nominatim for Slovenian places. Network errors are not fatal."""
    global _last_nominatim_call
    import requests  # local import keeps module import cheap

    wait = NOMINATIM_MIN_INTERVAL - (time.time() - _last_nominatim_call)
    if wait > 0:
        time.sleep(wait)
    params = {
        "q": query,
        "format": "jsonv2",
        "countrycodes": "si",
        "limit": 5,
        "accept-language": "sl",
    }
    _last_nominatim_call = time.time()
    response = requests.get(
        NOMINATIM_URL, params=params, headers={"User-Agent": NOMINATIM_UA}, timeout=15
    )
    response.raise_for_status()
    results = []
    for item in response.json():
        try:
            results.append(
                {
                    "label": _shorten(item["display_name"]),
                    "lat": float(item["lat"]),
                    "lon": float(item["lon"]),
                    "source": "osm",
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return results


def geocode_municipalities(query):
    """Match municipalities from OB.geojson (offline, always available).

    Results carry ``ob_name`` so the conversation can offer "follow the whole
    municipality" in addition to a radius around its centre.
    """
    needle = query.strip().upper()
    if not needle:
        return []
    exact = [
        name for name in OB_INDEX
        if name == needle or name == "OBČINA " + needle or "OBČINA " + name == needle
    ]
    loose = [name for name in OB_INDEX if needle in name and name not in exact]
    results = []
    for name in (exact + loose)[:5]:
        lat, lon = OB_INDEX[name]
        results.append(
            {
                "label": f"Občina {name.title()} (središče občine)",
                "lat": lat,
                "lon": lon,
                "source": "ob",
                "ob_name": name.title(),
            }
        )
    return results


def geocode(query):
    """Return up to five candidates: [{'label', 'lat', 'lon', 'source'}].

    Municipality hits from ``OB.geojson`` are appended (they are not on the
    Nominatim list for village names), each marked with ``ob_name`` so the user
    can follow the whole občina.
    """
    query = (query or "").strip()
    if len(query) < 2:
        return []
    cached = _cache_get(query)
    if cached:
        return cached
    candidates = []
    try:
        candidates = geocode_nominatim(query)
    except Exception as exc:
        logger.warning("Nominatim failed for %r: %s", query, exc)
    municipalities = geocode_municipalities(query)
    known = {c["label"] for c in candidates}
    for municipality in municipalities:
        if municipality["label"] in known:
            continue
        candidates.append(municipality)
        if len([c for c in candidates if c.get("ob_name")]) >= 2:
            break
    if not candidates:
        candidates = municipalities
    if candidates:
        _cache_put(query, candidates)
    return candidates


# --------------------------------------------------------------------------- #
# Telegram conversation
# --------------------------------------------------------------------------- #

def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _subscription_color(index):
    return MAP_COLORS[index % len(MAP_COLORS)]


def _emoji(index):
    return MAP_EMOJI[index % len(MAP_EMOJI)]


def circle_ring(lat, lon, radius_km, steps=96):
    """Approximate a radius circle as a closed [(lon, lat), ...] ring."""
    lat = float(lat)
    lon = float(lon)
    radius_km = max(float(radius_km), 0.1)
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 1e-6))
    return [
        (lon + dlon * math.cos(2 * math.pi * step / steps),
         lat + dlat * math.sin(2 * math.pi * step / steps))
        for step in range(steps)
    ]


def _project(static_map, lon, lat):
    """Project a (lon, lat) pair to the pixel position StaticMap will use."""
    from staticmap.staticmap import _lat_to_y, _lon_to_x

    return (
        static_map._x_to_px(_lon_to_x(float(lon), static_map.zoom)),
        static_map._y_to_px(_lat_to_y(float(lat), static_map.zoom)),
    )


def _draw_overlays(image, static_map, shapes, markers, fill_alpha=60, outline_width=3, scale=2):
    """Composite translucent fills, all borders and the markers onto a rendered map.

    Each fill goes onto its own layer and is alpha-composited in turn, so
    overlapping areas get deeper instead of replacing each other.  Every outline
    is drawn afterwards in a single pass, which keeps the border of an area that
    lies *under* another one fully visible.  Drawing happens at ``scale`` x and is
    downsampled, which is how staticmap gets its anti-aliasing.
    """
    from PIL import Image, ImageDraw

    canvas = image.convert("RGBA")
    rings = []
    for ring, fill, outline in shapes:
        points = [_project(static_map, lon, lat) for lon, lat in ring]
        points = [(x * scale, y * scale) for x, y in points]
        if len(points) >= 3:
            rings.append((points, tuple(fill), tuple(outline)))

    size = (static_map.width * scale, static_map.height * scale)
    for points, fill, _outline in rings:
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(points, fill=fill + (fill_alpha,))
        canvas = Image.alpha_composite(canvas, layer.resize(canvas.size, Image.LANCZOS))

    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for points, _fill, outline in rings:
        draw.line(points + [points[0]], fill=outline + (255,),
                  width=max(1, int(outline_width)) * scale, joint="curve")
    canvas = Image.alpha_composite(canvas, layer.resize(canvas.size, Image.LANCZOS))

    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for (lon, lat), rgb in markers:
        x, y = _project(static_map, lon, lat)
        x, y, radius = x * scale, y * scale, 7 * scale
        draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                     fill=tuple(rgb) + (255,))
    canvas = Image.alpha_composite(canvas, layer.resize(canvas.size, Image.LANCZOS))
    return canvas


def _render_plain_map(rows, path, width, height):
    """Fallback used when StaticMap's projection helpers are unavailable."""
    from staticmap import CircleMarker, Line, Polygon, StaticMap

    overview = StaticMap(
        width, height, padding_x=45, padding_y=45,
        url_template=MAP_STYLE_URLS.get("topo", MAP_STYLE_URLS["default"]),
    )
    for index, row in enumerate(rows):
        rgb = _hex_to_rgb(_subscription_color(index))
        fill = rgb + (60,)
        outline = rgb + (255,)
        if (row.get("kind") or "circle") == "obcina":
            key = (row.get("ob_name") or "").strip().upper()
            rings = OB_RINGS.get(key) or []
            for ring in rings:
                if len(ring) < 3:
                    continue
                overview.add_polygon(Polygon(ring, fill, outline))
                overview.add_line(Line(list(ring) + [ring[0]], outline, 3))
        elif (row.get("kind") or "circle") == "geohash":
            box = geohash_bbox(row.get("geohash"))
            if box:
                ring = box_ring(box)
                overview.add_polygon(Polygon(ring, fill, outline))
                overview.add_line(Line(ring, outline, 3))
        else:
            ring = circle_ring(row["lat"], row["lon"], row.get("radius_km") or MIN_RADIUS_KM)
            overview.add_polygon(Polygon(ring, fill, outline))
            overview.add_line(Line(ring + [ring[0]], outline, 3))
        overview.add_marker(CircleMarker((row["lon"], row["lat"]), outline, 7))
    overview.render().save(path)
    return path


def render_subscriptions_map(rows, path, width=1000, height=760):
    """Draw every followed location (radius circles and municipality areas) on one map.

    Areas are filled translucently and every outline is drawn in a final pass, so
    a place that sits inside a followed municipality (or two radii that overlap)
    still shows the border of the area underneath.
    """
    from staticmap import Polygon, StaticMap

    if not rows:
        raise ValueError("nothing to draw")

    shapes = []
    markers = []
    for index, row in enumerate(rows):
        rgb = _hex_to_rgb(_subscription_color(index))
        if (row.get("kind") or "circle") == "obcina":
            key = (row.get("ob_name") or "").strip().upper()
            for ring in OB_RINGS.get(key) or []:
                if len(ring) >= 3:
                    shapes.append((ring, rgb, rgb))
        elif (row.get("kind") or "circle") == "geohash":
            box = geohash_bbox(row.get("geohash"))
            if box:
                shapes.append((box_ring(box), rgb, rgb))
        else:
            shapes.append((circle_ring(row["lat"], row["lon"],
                                       row.get("radius_km") or MIN_RADIUS_KM), rgb, rgb))
        markers.append(((float(row["lon"]), float(row["lat"])), rgb))

    overview = StaticMap(
        width, height, padding_x=45, padding_y=45,
        url_template=MAP_STYLE_URLS.get("topo", MAP_STYLE_URLS["default"]),
    )
    # Outlines/colors are empty on purpose: the polygons are only registered so
    # StaticMap computes the same zoom and centre for them; we draw them below.
    for ring, _fill, _outline in shapes:
        overview.add_polygon(Polygon(ring, None, None))

    try:
        base = overview.render()
        _draw_overlays(base, overview, shapes, markers).convert("RGB").save(path)
    except Exception:  # pragma: no cover - only if staticmap internals change
        logger.warning("overlay drawing failed, falling back to flat polygons", exc_info=True)
        return _render_plain_map(rows, path, width, height)
    return path


HELP_TEXT = (
    "<b>SPIN112 – obveščanje po lokaciji</b>\n\n"
    "Nastavite mesta, vasi ali cele občine, ki vas zanimajo, in vsako novo "
    "intervencijo znotraj tega območja vam pošljem tukaj, v zasebnem klepetu – "
    "z zemljevidom vred. Skupinska sporočila v kanalu ostanejo nespremenjena; "
    "to je vaš osebni filter.\n\n"
    "<b>Ukazi</b>\n"
    "<b>/track &lt;kraj&gt;</b> – dodaj lokacijo po imenu. Bot poišče kraj, vi "
    "izberete pravega in nato radij (od 1 do 100 km; 1 km je namenjen "
    "sledenju ene same ulice ali zaselka).\n"
    "<b>/track &lt;širina&gt; &lt;dolžina&gt; [radij]</b> – dodaj točno točko po "
    "koordinatah, npr. <code>/track 46.2397 14.3567 5</code> (širina, dolžina, "
    "radij v km). Radij lahko izpustite in bot vas bo vprašal zanj. "
    "Vrstni red je vedno <i>širina (lat) dolžina (lon) radij</i>; decimalna "
    "pika ali vejica sta enako veljavni.\n"
    "<b>/track gh:&lt;geohash&gt;</b> – dodaj celico po geohashu, npr. "
    "<code>/track gh:u24q4b</code> (Ljubljana). Geohash je kratek niz iz črk in "
    "številk (glej <a href=\"https://en.wikipedia.org/wiki/Geohash\">Wikipedijo</a> "
    "ali katerekoli geohash aplikacije); daljši niz "
    "pomeni manjše območje. Spremlja se <b>točno ta celica</b> – geohash je "
    "pravokotnik, zato radija ni in bot vas zanj ne vpraša. Geohash brez "
    "predpone <code>gh:</code> prepozna tudi takrat, ko kraja z istim imenom ne "
    "najde.\n"
    "<b>/track Občina Ptuj</b> – pri občinah bot ponudi tudi »cela občina«, "
    "brez radija: obvestilo dobiš, kadar je intervencija kjerkoli znotraj meje "
    "občine.\n"
    "<b>/list</b> – prikaže vse vaše lokacije <i>na zemljevidu</i>, z narisanimi "
    "radiji in mejami občin, ter gumbe za brisanje. Območja so narisana "
    "prosojno, zato ostanejo vidne tudi meje območij, ki se prekrivajo.\n"
    "<b>/remove &lt;številka&gt;</b> – odstrani lokacijo s tega seznama; bot "
    "vedno še enkrat vpraša za potrditev.\n"
    "<b>/stop</b> – izbriše vse vaše lokacije (z potrditvijo).\n"
    "<b>/cancel</b> – prekini trenutni korak. /help – ta pomoč.\n\n"
    "<b>Imena lokacij</b>\n"
    "Vsaki lokaciji lahko daste svoje ime (»Doma«, »Služba«, »Vikend«). Ko "
    "vnesete koordinate, bot vedno vpraša, kako naj jo imenuje – ime se pojavi "
    "v /list in v obvestilih, da takoj veste, zakaj ste sporočilo dobili.\n\n"
    "<b>Vrste dogodkov (teme)</b>\n"
    "Vsaki lokaciji lahko določite, katere vrste dogodkov vas zanimajo – npr. samo "
    "<i>Epidemije</i>, <i>Jedrska ali radiološka nevarnost</i>, "
    "<i>Športne aktivnosti</i> ali <i>Požar, eksplozija</i>. Bot vas vpraša takoj "
    "po izbiri lokacije; izberete lahko več vrst, s klikom na vrsto jo "
    "vklopite/izklopite. Če ne izberete nobene, prejmete <b>vse</b> dogodke v tem "
    "območju – enako kot doslej.\n"
    "<b>/topics</b> – spremenite vrste za že shranjeno lokacijo "
    "(<code>/topics 3</code> ali izberite s seznama).\n"
    "Na voljo: Epidemije · Jedrska ali radiološka nevarnost · Požar, eksplozija · "
    "Prometna nesreča · Onesnaženje in nevarne snovi · Nevarne snovi (ključne "
    "besede) · Tehnična in druga pomoč (tudi jama in plaz) · Najdbe NUS · Motnje "
    "oskrbe · Prevoz pitne vode · Lažne intervencije · Gore · Športne "
    "aktivnosti.\n\n"
    "<b>Primeri</b>\n"
    "<code>/track Ljubljana</code>\n"
    "<code>/track Videm pri Ptuju</code>\n"
    "<code>/track 46.2397 14.3567 10</code>\n"
    "<code>/track 46.2397 14.3567</code> – radij vpraša naslednji korak\n"
    "<code>/topics 2</code> – spremeni vrste dogodkov za 2. lokacijo\n\n"
    "Obseg: radij od 1 do 100 km, največ 15 lokacij na klepet."
)


def _private_only(update):
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def _refuse_group(update, context):
    await update.effective_message.reply_text(
        "Ta ukaz deluje samo v zasebnem klepetu z botom. Pišite mi v zasebno sporočilo."
    )


def _radius_keyboard(ob_name=None):
    """Radius presets; municipalities get a whole-municipality row."""
    buttons = [
        InlineKeyboardButton(f"{km} km", callback_data=f"rad:{km}") for km in RADIUS_PRESETS
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton("✏️ drugo", callback_data="rad:custom")])
    if ob_name:
        rows.append(
            [InlineKeyboardButton(f"🗺 cela občina {ob_name}", callback_data="rad:obc")]
        )
    return InlineKeyboardMarkup(rows)


async def _answer(update, text, keyboard=None, from_callback=False, parse_mode="HTML"):
    """Reply to a message or edit the callback message, whichever applies."""
    if from_callback and update.callback_query is not None:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=keyboard
            )
            return
        except TelegramError:
            pass  # callback is too old to edit; fall through to a fresh message
    await update.effective_message.reply_text(
        text, parse_mode=parse_mode, reply_markup=keyboard
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return
    await update.effective_message.reply_text(
        "Pozdravljeni! Tukaj lahko izberete lokacije, o katerih želite prejemati obvestila "
        "o intervencijah.\n\n" + HELP_TEXT,
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return await _refuse_group(update, context)
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="HTML")


_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
_NUMBERS_RE = re.compile(r"[+-]?\d+(?:[.,]\d+)?")


def parse_coordinates(text):
    """Parse '46.2397 14.3567 5' -> (lat, lon, radius_km_or_None), else None.

    Accepts two or three numbers separated by spaces, commas or semicolons,
    with either a decimal point or a decimal comma. Order is lat, lon, radius.
    """
    tokens = _NUMBERS_RE.findall((text or "").strip())
    if not 2 <= len(tokens) <= 3:
        return None
    if not all(_NUMBER_RE.match(token) for token in tokens):
        return None
    leftover = (text or "").strip()
    for token in tokens:
        leftover = leftover.replace(token, "", 1)
    if not re.fullmatch(r"[\s,;:°/]*", leftover):
        return None  # stray words, so this is not a bare coordinate pair
    values = [float(token.replace(",", ".")) for token in tokens]
    if not all(abs(value) <= 360 for value in values):
        return None
    lat, lon = values[0], values[1]
    radius = values[2] if len(values) == 3 else None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if not _in_slovenia(lat, lon) and _in_slovenia(lon, lat):
        lat, lon = lon, lat  # tolerate "lon lat" input
    return lat, lon, radius


def _in_slovenia(lat, lon):
    return SI_LAT_MIN <= lat <= SI_LAT_MAX and SI_LON_MIN <= lon <= SI_LON_MAX


def is_geohash(text, min_len=GEOHASH_MIN_LEN):
    """True when ``text`` is nothing but geohash characters of a sane length."""
    token = (text or "").strip().lower()
    return (
        GEOHASH_MIN_LEN <= len(token) <= GEOHASH_MAX_LEN
        and _GEOHASH_RE.match(token) is not None
    ) and len(token) >= min_len


def decode_geohash(geohash):
    """Decode a geohash to (lat, lon, lat_err, lon_err) or None if invalid.

    ``lat_err``/``lon_err`` are the half-sizes of the cell in degrees, so the
    cell spans ``lat ± lat_err`` and ``lon ± lon_err``.
    """
    token = (geohash or "").strip().lower()
    if not token or not _GEOHASH_RE.match(token) or len(token) > GEOHASH_MAX_LEN:
        return None
    lat_min, lat_max, lon_min, lon_max = -90.0, 90.0, -180.0, 180.0
    even = True  # geohash interleaves bits, starting with longitude
    for char in token:
        index = GEOHASH_INDEX[char]
        for bit in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_min + lon_max) / 2
                if index & bit:
                    lon_min = mid
                else:
                    lon_max = mid
            else:
                mid = (lat_min + lat_max) / 2
                if index & bit:
                    lat_min = mid
                else:
                    lat_max = mid
            even = not even
    return (
        (lat_min + lat_max) / 2,
        (lon_min + lon_max) / 2,
        (lat_max - lat_min) / 2,
        (lon_max - lon_min) / 2,
    )


def encode_geohash(lat, lon, precision=6):
    """Encode a point to a geohash of ``precision`` characters."""
    precision = max(GEOHASH_MIN_LEN, min(GEOHASH_MAX_LEN, int(precision)))
    lat_min, lat_max, lon_min, lon_max = -90.0, 90.0, -180.0, 180.0
    bits, even = [], True
    while len(bits) < precision * 5:
        if even:
            mid = (lon_min + lon_max) / 2
            if lon >= mid:
                bits.append(1)
                lon_min = mid
            else:
                bits.append(0)
                lon_max = mid
        else:
            mid = (lat_min + lat_max) / 2
            if lat >= mid:
                bits.append(1)
                lat_min = mid
            else:
                bits.append(0)
                lat_max = mid
        even = not even
    out = []
    for start in range(0, len(bits), 5):
        value = 0
        for bit in bits[start:start + 5]:
            value = (value << 1) | bit
        out.append(GEOHASH_ALPHABET[value])
    return "".join(out)


def geohash_bbox(geohash):
    """Decode a cell to (lat_min, lat_max, lon_min, lon_max), or None if invalid.

    A geohash cell is a rectangle in lat/lon, so this is the watch area itself –
    there is no radius anywhere in the geohash path.
    """
    decoded = decode_geohash(geohash)
    if not decoded:
        return None
    lat, lon, lat_err, lon_err = decoded
    return (lat - lat_err, lat + lat_err, lon - lon_err, lon + lon_err)


def point_in_box(lat, lon, box):
    """True when the point falls inside a (lat_min, lat_max, lon_min, lon_max) box."""
    lat_min, lat_max, lon_min, lon_max = box
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def box_ring(box):
    """Closed [(lon, lat), ...] ring that draws a lat/lon box as a rectangle."""
    lat_min, lat_max, lon_min, lon_max = box
    return [
        (lon_min, lat_min),
        (lon_max, lat_min),
        (lon_max, lat_max),
        (lon_min, lat_max),
        (lon_min, lat_min),
    ]


def geohash_cell_size_km(geohash):
    """Approximate (width_km, height_km) of a cell, for user-facing messages."""
    box = geohash_bbox(geohash)
    if not box:
        return None
    lat_min, lat_max, lon_min, lon_max = box
    mid = (lat_min + lat_max) / 2
    height = (lat_max - lat_min) * 111.32
    width = (lon_max - lon_min) * 111.32 * max(math.cos(math.radians(mid)), 1e-6)
    return width, height


def parse_geohash_input(text, require_prefix=False):
    """Parse 'gh:u24q4b', 'geohash u24q4b' or a bare 'u24q4b'.

    Returns a dict with the decoded centre, the exact cell rectangle (``bbox``)
    and its size. A trailing number is still accepted so that old habits do not
    break, but geohash cells are rectangles, so the caller ignores it.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    explicit = False
    match = _GEOHASH_PREFIX_RE.match(raw)
    if match:
        explicit = True
        raw = raw[match.end():].strip()
    elif require_prefix:
        return None
    tokens = [token for token in re.split(r"[\s;]+", raw) if token]
    # "gh:u24q4b,10" is a hash and a radius, while "gh:u24q4b 2,5" uses a comma
    # as the decimal mark; both should end up as exactly two tokens.
    if len(tokens) == 1 and "," in tokens[0]:
        head, _, tail = tokens[0].partition(",")
        if is_geohash(head) and _NUMBER_RE.match(tail):
            tokens = [head, tail]
    if not 1 <= len(tokens) <= 2:
        return None
    token = tokens[0].lower()
    if not is_geohash(token):
        return None
    radius = None
    if len(tokens) == 2:
        if not _NUMBER_RE.match(tokens[1]):
            return None
        radius = float(tokens[1].replace(",", "."))
        if radius <= 0:
            return None
    decoded = decode_geohash(token)
    if not decoded:
        return None
    lat, lon, _lat_err, _lon_err = decoded
    return {
        "geohash": token,
        "lat": lat,
        "lon": lon,
        "bbox": geohash_bbox(token),
        "size_km": geohash_cell_size_km(token),
        "radius_km": radius,
        "explicit": explicit,
    }


def _clear_pending(context):
    for key in ("pending", "candidate", "candidates", "pending_query",
                "topic_selection", "topic_edit", "topic_header"):
        context.user_data.pop(key, None)


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        await _refuse_group(update, context)
        return ConversationHandler.END
    query = " ".join(context.args or []).strip()
    if not query:
        await update.effective_message.reply_text(
            "Katero mesto, vas ali občino želite spremljati?\n"
            "Pošljite ime (<i>Ljubljana</i>, <i>Videm pri Ptuju</i>), koordinate "
            "(<code>46.2397 14.3567 5</code> – širina, dolžina, radij v km) ali "
            "geohash (<code>gh:u24q4b</code> – spremlja se točno ta celica, "
            "brez radija).\n"
            "Za preklic: /cancel",
            parse_mode="HTML",
        )
        return PLACE
    return await _start_from_text(update, context, query, from_callback=False)


async def _start_from_text(update, context, text, from_callback=False):
    """Route user input: geohash, coordinate triple, or a place name."""
    geohash = parse_geohash_input(text, require_prefix=True)
    if geohash is not None:
        return _state_for(await _use_geohash(update, context, geohash, from_callback))
    parsed = parse_coordinates(text)
    if parsed is None:
        return await _show_candidates(update, context, text)
    outcome = await _use_coordinates(update, context, parsed, from_callback)
    if outcome == "name":
        return NAME
    return RADIUS if outcome == "radius" else PLACE


def _state_for(outcome):
    """Map an _use_* outcome to the conversation state to wait in."""
    if outcome == "name":
        return NAME
    return RADIUS if outcome == "radius" else PLACE


async def _use_coordinates(update, context, parsed, from_callback=False):
    """Store a coordinate point; ask for the radius and/or the name.

    Returns 'invalid', 'radius' or 'name'.
    """
    lat, lon, radius = parsed
    label = f"{lat:.4f}, {lon:.4f}"
    if not _in_slovenia(lat, lon):
        await _answer(
            update,
            f"Koordinate <code>{lat:.4f}, {lon:.4f}</code> niso znotraj Slovenije.\n"
            "Vrstni red je <b>širina (lat) dolžina (lon) radij</b> – npr. "
            "<code>46.2397 14.3567 5</code>. Poskusite znova ali /cancel.",
            from_callback=from_callback,
        )
        return "invalid"
    context.user_data["pending"] = {
        "label": label, "lat": lat, "lon": lon, "kind": "circle", "ob_name": None,
    }
    if radius is None:
        await _answer(
            update,
            f"📍 Točka <b>{label}</b>.\nV katerem obsegu naj spremljam to lokacijo?",
            _radius_keyboard(),
            from_callback,
        )
        return "radius"
    if not (MIN_RADIUS_KM <= radius <= MAX_RADIUS_KM):
        await _answer(
            update,
            f"Radij mora biti med {MIN_RADIUS_KM:g} in {MAX_RADIUS_KM:g} km. "
            f"Vpišite radij za točko {label} (npr. 10).",
            from_callback=from_callback,
        )
        return "radius"
    context.user_data["pending"]["radius_km"] = radius
    await _ask_name(update, context, from_callback)
    return "name"


async def _use_geohash(update, context, parsed, from_callback=False, implicit=False):
    """Store a geohash cell as the rectangle it is – no radius is asked for.

    Returns 'invalid' or 'name'.
    """
    geohash = parsed["geohash"]
    lat, lon = parsed["lat"], parsed["lon"]
    box = parsed.get("bbox") or geohash_bbox(geohash)
    size = parsed.get("size_km") or geohash_cell_size_km(geohash) or (0.0, 0.0)
    width_km, height_km = size
    if not box:
        await _answer(
            update,
            f"Geohash <code>{html.escape(geohash)}</code> ni veljaven. Poskusite znova ali /cancel.",
            from_callback=from_callback,
        )
        return "invalid"
    if len(geohash) < GEOHASH_WATCH_MIN_LEN:
        await _answer(
            update,
            f"Geohash <code>{html.escape(geohash)}</code> je prevelik za spremljanje – "
            f"celica meri ≈{width_km:.0f} × {height_km:.0f} km. Dodajte vsaj "
            f"{GEOHASH_WATCH_MIN_LEN - len(geohash)} znakov (npr. "
            f"<code>{encode_geohash(lat, lon, GEOHASH_WATCH_MIN_LEN)}</code> "
            "za območje velikosti mesta). Poskusite znova ali /cancel.",
            from_callback=from_callback,
        )
        return "invalid"
    if not _in_slovenia(lat, lon):
        await _answer(
            update,
            f"Središče geohasha <code>{html.escape(geohash)}</code> "
            f"(<code>{lat:.4f}, {lon:.4f}</code>) ni v Sloveniji. "
            "Poskusite znova ali /cancel.",
            from_callback=from_callback,
        )
        return "invalid"
    detected = " (prebral sem ga kot geohash)" if implicit else ""
    context.user_data.pop("candidate", None)
    context.user_data["pending"] = {
        "label": f"Geohash {geohash}",
        "lat": lat,
        "lon": lon,
        "kind": "geohash",
        "ob_name": None,
        "geohash": geohash,
        "bbox": box,
        "size_km": (width_km, height_km),
    }
    lat_min, lat_max, lon_min, lon_max = box
    note = ""
    if parsed.get("radius_km"):
        note = "\nRadij pri geohashu ne velja – spremljam točno to celico."
    await _answer(
        update,
        f"🔲 Geohash <code>{html.escape(geohash)}</code>{detected}\n"
        f"Celica (pravokotnik) ≈{width_km:.1f} × {height_km:.1f} km: "
        f"<code>{lat_min:.4f}, {lon_min:.4f}</code> – "
        f"<code>{lat_max:.4f}, {lon_max:.4f}</code>.{note}",
        from_callback=from_callback,
    )
    await _ask_name(update, context, from_callback)
    return "name"


async def _ask_name(update, context, from_callback=False):
    """Always ask what the new location should be called."""
    pending = context.user_data.get("pending") or {}
    suggestion = (pending.get("label") or "")[:MAX_LABEL_LEN]
    details = []
    if pending.get("label"):
        details.append(f"📍 <code>{html.escape(pending['label'])}</code>")
    if pending.get("geohash"):
        details.append(f"🔲 celica <code>{html.escape(pending['geohash'])}</code> "
                       "(pravokotnik, brez radija)")
    elif pending.get("radius_km"):
        details.append(f"radij {pending['radius_km']:g} km")
    if pending.get("ob_name"):
        details.append(f"občina {html.escape(pending['ob_name'])}")
    header = " · ".join(details) if details else "📍 nova lokacija"
    keyboard = None
    if suggestion:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Uporabi »{suggestion}«", callback_data="name:loc")
        ]])
    await _answer(
        update,
        f"{header}\n\n<b>Kako naj imenujem to lokacijo?</b>\n"
        "Vpišite ime – npr. <i>Doma</i>, <i>Služba</i>, <i>Vikend</i>, <i>Kočevje</i>. "
        "Ime se bo prikazalo na seznamu (/list) in v vsakem obvestilu.",
        keyboard,
        from_callback,
    )


async def got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.effective_message.text or "").strip()
    if len(raw) < 2:
        await update.effective_message.reply_text("Ime je prekratko. Vpišite vsaj 2 znaka ali /cancel.")
        return NAME
    if len(raw) > MAX_LABEL_LEN:
        await update.effective_message.reply_text(
            f"Ime je predolgo (največ {MAX_LABEL_LEN} znakov). Poskusite znova."
        )
        return NAME
    context.user_data.setdefault("pending", {})["label"] = raw
    return await _ask_topics(update, context, from_callback=False)


async def name_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await _ask_topics(update, context, from_callback=True)


async def _commit(update, context, label, from_callback=False, topics=None):
    """Persist the pending location under ``label`` and confirm."""
    pending = context.user_data.get("pending")
    chat_id = update.effective_chat.id
    if not pending:
        await _answer(
            update, "Nekaj je šlo narobe. Začnite znova z /track.", from_callback=from_callback
        )
        return ConversationHandler.END
    existing = await asyncio.to_thread(list_subscriptions, chat_id)
    same_label = any(row["label"] == label for row in existing)
    if not same_label and len(existing) >= MAX_SUBSCRIPTIONS:
        await _answer(
            update,
            f"Dosegli ste največ {MAX_SUBSCRIPTIONS} lokacij. Odstranite eno z /remove.",
            from_callback=from_callback,
        )
        _clear_pending(context)
        return ConversationHandler.END
    kind = pending.get("kind", "circle")
    radius_km = pending.get("radius_km") or 0.0
    geohash = pending.get("geohash")
    await asyncio.to_thread(
        add_subscription, chat_id, label, pending["lat"], pending["lon"],
        radius_km, kind, pending.get("ob_name"), topics, geohash,
    )
    if kind == "obcina":
        scope = f"cela občina <b>{html.escape(pending.get('ob_name') or '')}</b>"
    elif kind == "geohash" or geohash:
        scope = (
            f"celica geohasha <code>{html.escape(geohash or '')}</code> "
            "(pravokotnik, brez radija)"
        )
    else:
        scope = f"radij {radius_km:g} km"
    verb = "Posodobljeno" if same_label else "Dodano"
    await _answer(
        update,
        f"✅ {verb}: <b>{html.escape(label)}</b> – {scope}.\n"
        f"📍 <code>{pending['lat']:.4f}, {pending['lon']:.4f}</code>\n"
        f"🏷 Vrste: <b>{html.escape(describe_topics({'topics': encode_topics(topics)}))}</b>\n"
        "Nove intervencije v tem območju vam pošljem tukaj. Vse lokacije in "
        "zemljevid: /list. Vrste kadarkoli spremenite s /topics.",
        from_callback=from_callback,
    )
    _clear_pending(context)
    return ConversationHandler.END


async def _show_candidates(update, context, query):
    context.user_data["pending_query"] = query
    candidates = await asyncio.to_thread(geocode, query)
    if not candidates:
        geohash = parse_geohash_input(query)
        if geohash is not None:
            outcome = await _use_geohash(update, context, geohash, implicit=True)
            return _state_for(outcome)
        await update.effective_message.reply_text(
            f"Za »{html.escape(query)}« nisem našel nobenega kraja v Sloveniji.\n"
            "Poskusite z drugačnim imenom, vnesite koordinate "
            "(<code>46.2397 14.3567 5</code>) ali geohash (<code>gh:u24q4b</code>)."
        )
        return PLACE
    if len(candidates) == 1:
        context.user_data["candidate"] = candidates[0]
        await update.effective_message.reply_text(
            f"Najdeno: <b>{html.escape(candidates[0]['label'])}</b>\n"
            f"V katerem obsegu naj spremljam to lokacijo?",
            parse_mode="HTML",
            reply_markup=_radius_keyboard(candidates[0].get("ob_name")),
        )
        return RADIUS
    context.user_data["candidates"] = candidates
    keyboard = [
        [InlineKeyboardButton(c["label"][:60], callback_data=f"cand:{index}")]
        for index, c in enumerate(candidates)
    ]
    await update.effective_message.reply_text(
        f"Našel sem več krajev za »{html.escape(query)}«. Kateri je pravi?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PICK


async def got_place(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.effective_message.text or "").strip()
    if len(query) < 2:
        await update.effective_message.reply_text("Ime je prekratko. Poskusite znova ali /cancel.")
        return PLACE
    return await _start_from_text(update, context, query)


async def picked_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        index = int(query.data.split(":", 1)[1])
        candidate = context.user_data["candidates"][index]
    except (IndexError, KeyError, ValueError):
        await query.edit_message_text("Izbor je potekel. Začnite znova z /track.")
        return ConversationHandler.END
    context.user_data["candidate"] = candidate
    await query.edit_message_text(
        f"Izbrali ste <b>{html.escape(candidate['label'])}</b>.\n"
        f"V katerem obsegu naj spremljam to lokacijo?",
        parse_mode="HTML",
        reply_markup=_radius_keyboard(candidate.get("ob_name")),
    )
    return RADIUS


async def picked_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "custom":
        await query.edit_message_text(
            f"Vpišite radij v kilometrih (med {MIN_RADIUS_KM:g} in {MAX_RADIUS_KM:g}), npr. 15."
        )
        return RADIUS_CUSTOM
    candidate = context.user_data.get("candidate") or {}
    if value == "obc":
        ob_name = candidate.get("ob_name")
        if not ob_name:
            await query.edit_message_text("Za to lokacijo ne poznam meje občine. Izberite radij.")
            return RADIUS
        context.user_data["pending"] = {
            "label": f"Občina {ob_name}",
            "lat": candidate["lat"],
            "lon": candidate["lon"],
            "kind": "obcina",
            "ob_name": ob_name,
            "radius_km": 0.0,
        }
        return await _ask_topics(update, context, from_callback=True)
    try:
        radius_km = float(value)
    except ValueError:
        await query.edit_message_text("Napačna vrednost. Začnite znova z /track.")
        return ConversationHandler.END
    return await _save_from_candidate(update, context, radius_km, from_callback=True)


async def got_custom_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.effective_message.text or "").replace(",", ".").strip()
    try:
        radius_km = float(raw)
    except ValueError:
        await update.effective_message.reply_text(
            "To ni število. Vpišite radij v kilometrih, npr. 15."
        )
        return RADIUS_CUSTOM
    if not (MIN_RADIUS_KM <= radius_km <= MAX_RADIUS_KM):
        await update.effective_message.reply_text(
            f"Radij mora biti med {MIN_RADIUS_KM:g} in {MAX_RADIUS_KM:g} km."
        )
        return RADIUS_CUSTOM
    if context.user_data.get("pending") and not context.user_data.get("candidate"):
        # radius for a coordinate entry: remember it, then always ask for a name
        context.user_data["pending"]["radius_km"] = radius_km
        await _ask_name(update, context)
        return NAME
    return await _save_from_candidate(update, context, radius_km, from_callback=False)


async def _save_from_candidate(update, context, radius_km, from_callback):
    candidate = context.user_data.get("candidate")
    if not candidate:
        context.user_data["pending"]["radius_km"] = radius_km
        await _ask_name(update, context, from_callback)
        return NAME
    context.user_data["pending"] = {
        "label": candidate["label"],
        "lat": candidate["lat"],
        "lon": candidate["lon"],
        "kind": "circle",
        "ob_name": candidate.get("ob_name"),
        "radius_km": radius_km,
    }
    return await _ask_topics(update, context, from_callback=from_callback)


def _pending_header(pending):
    """One-line summary of the location currently being configured."""
    label = html.escape(pending.get("label") or "nova lokacija")
    if (pending.get("kind") or "circle") == "obcina":
        return f"📍 <b>{label}</b> · cela občina {html.escape(pending.get('ob_name') or '')}"
    return f"📍 <b>{label}</b> · radij {(pending.get('radius_km') or 0):g} km"


def _topic_keyboard(selected):
    selected = list(selected or [])
    rows, row = [], []
    for index, (key, label) in enumerate(TOPIC_CHOICES):
        mark = "✅ " if key in selected else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"top:{index}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 Vse vrste (brez filtra)", callback_data="top:all")])
    rows.append([InlineKeyboardButton("💾 Shrani izbor", callback_data="top:save")])
    return InlineKeyboardMarkup(rows)


def _topic_prompt(header, selected):
    if selected:
        names = ", ".join(TOPIC_LABELS[key].split(" ", 1)[-1] for key in selected)
        state = f"Izbrali ste <b>{len(selected)}</b>: {html.escape(names)}"
    else:
        state = "Trenutno: <b>vse vrste dogodkov</b> – prejmete vse."
    return (
        f"{header}\n\n<b>Katere vrste dogodkov vas zanimajo?</b>\n"
        "Klikajte po vrsti – izberete lahko več. Če ne izberete nobene, prejmete "
        "<b>vse</b> dogodke v tem območju.\n\n"
        f"{state}\n\n"
        "Primeri: <i>Epidemije</i>, <i>Jedrska ali radiološka nevarnost</i>, "
        "<i>Športne aktivnosti</i>."
    )


async def _ask_topics(update, context, from_callback=False):
    """Ask which incident kinds this location should report; returns TOPICS."""
    pending = context.user_data.get("pending") or {}
    context.user_data["topic_header"] = _pending_header(pending)
    context.user_data["topic_selection"] = list(pending.get("topics") or [])
    selected = context.user_data["topic_selection"]
    await _answer(
        update,
        _topic_prompt(context.user_data["topic_header"], selected),
        _topic_keyboard(selected),
        from_callback=from_callback,
    )
    return TOPICS


async def picked_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle one incident kind, clear the filter, or save the selection."""
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "save":
        return await _save_topics(update, context)
    selected = list(context.user_data.get("topic_selection") or [])
    if value == "all":
        selected = []
    else:
        try:
            key = TOPIC_CHOICES[int(value)][0]
        except (IndexError, ValueError):
            return TOPICS
        if key in selected:
            selected.remove(key)
        else:
            selected.append(key)
    context.user_data["topic_selection"] = selected
    try:
        await query.edit_message_text(
            _topic_prompt(context.user_data.get("topic_header") or "", selected),
            parse_mode="HTML",
            reply_markup=_topic_keyboard(selected),
        )
    except TelegramError:
        pass
    return TOPICS


async def _save_topics(update, context):
    """Persist the selection: new location -> commit, existing one -> update."""
    selected = list(context.user_data.get("topic_selection") or [])
    sub_id = context.user_data.get("topic_edit")
    if sub_id is not None:
        chat_id = update.effective_chat.id
        row = await asyncio.to_thread(get_subscription, chat_id, sub_id)
        if not row:
            await _answer(update, "Take lokacije ni več na seznamu.", from_callback=True)
            _clear_pending(context)
            return ConversationHandler.END
        await asyncio.to_thread(set_topics, chat_id, sub_id, selected)
        tags = describe_topics({"topics": encode_topics(selected)})
        await _answer(
            update,
            f"✅ Vrste za <b>{html.escape(row['label'])}</b>: <b>{html.escape(tags)}</b>.\n"
            "Obvestila za to območje bodo upoštevala nov izbor.",
            from_callback=True,
        )
        _clear_pending(context)
        return ConversationHandler.END
    pending = context.user_data.get("pending") or {}
    return await _commit(
        update, context, pending.get("label", ""), from_callback=True, topics=selected
    )


async def _start_topic_edit(update, context, sub_id, from_callback=False):
    """Open the kind picker for an already saved location."""
    chat_id = update.effective_chat.id
    row = await asyncio.to_thread(get_subscription, chat_id, sub_id)
    if not row:
        await _answer(
            update, "Take lokacije ni na vašem seznamu. Glejte /list.",
            from_callback=from_callback,
        )
        return TOPIC_PICK
    context.user_data["topic_edit"] = sub_id
    context.user_data["topic_selection"] = decode_topics(row.get("topics"))
    context.user_data["topic_header"] = (
        f"🏷 <b>{html.escape(row['label'])}</b> · {describe_subscription(row)}"
    )
    selected = context.user_data["topic_selection"]
    await _answer(
        update,
        _topic_prompt(context.user_data["topic_header"], selected),
        _topic_keyboard(selected),
        from_callback=from_callback,
    )
    return TOPICS


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change which incident kinds a saved location reports."""
    if not _private_only(update):
        return await _refuse_group(update, context)
    chat_id = update.effective_chat.id
    if context.args and context.args[0].isdigit():
        return await _start_topic_edit(update, context, int(context.args[0]))
    rows = await asyncio.to_thread(list_subscriptions, chat_id)
    if not rows:
        await update.effective_message.reply_text(
            "Nimate shranjenih lokacij. Dodajte jih s /track.", parse_mode="HTML"
        )
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton(
            f"🏷 {row['label'][:30]} · {describe_topics(row)}"[:60],
            callback_data=f"tsel:{row['id']}",
        )]
        for row in rows
    ]
    await update.effective_message.reply_text(
        "<b>Kateri lokaciji želite spremeniti vrste dogodkov?</b>\n"
        "Izberite lokacijo ali uporabite <code>/topics &lt;številka&gt;</code>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return TOPIC_PICK


async def picked_topic_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        sub_id = int(query.data.split(":", 1)[1])
    except ValueError:
        return TOPIC_PICK
    return await _start_topic_edit(update, context, sub_id, from_callback=True)


async def got_topic_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.effective_message.text or "").strip()
    if not raw.isdigit():
        await update.effective_message.reply_text(
            "Vpišite številko lokacije s seznama (/list) ali /cancel."
        )
        return TOPIC_PICK
    return await _start_topic_edit(update, context, int(raw))


def _list_caption(rows):
    lines = [f"<b>Vaše lokacije ({len(rows)}):</b>"]
    for index, row in enumerate(rows):
        scope = describe_subscription(row)
        obcina = (row.get("kind") or "circle") == "obcina"
        coords = "" if obcina else f" · {row['lat']:.4f}, {row['lon']:.4f}"
        tags = describe_topics(row)
        kinds = "" if tags == ALL_TOPICS_TEXT else f" · 🏷 {html.escape(tags)}"
        lines.append(
            f"{kind_emoji(row, index)} <code>{row['id']}</code> · <b>{html.escape(row['label'])}</b>"
            f" · {scope}{coords}{kinds}"
        )
    lines.append("")
    lines.append(
        "Barva vsake vrstice se ujema z njenim območjem na zemljevidu, "
        "oblika pa pove vrsto: ❤️ občina (meja), 🟥 geohash celica, "
        "🔴 območje z radijem. Odstranitev: /remove &lt;številka&gt; "
        "ali gumb spodaj. Vrste dogodkov: /topics."
    )
    return "\n".join(lines)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return await _refuse_group(update, context)
    chat_id = update.effective_chat.id
    rows = await asyncio.to_thread(list_subscriptions, chat_id)
    if not rows:
        await update.effective_message.reply_text(
            "Nimate shranjenih lokacij. Dodajte jih s /track – z imenom kraja ali s "
            "koordinatami (npr. <code>/track 46.2397 14.3567 5</code>).",
            parse_mode="HTML",
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"❌ {row['label'][:40]}", callback_data=f"del:{row['id']}")]
        for row in rows
    ]
    markup = InlineKeyboardMarkup(keyboard)
    caption = _list_caption(rows)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscriber_map.png")
    try:
        await asyncio.to_thread(render_subscriptions_map, rows, path)
    except Exception as exc:  # tiles unreachable, staticmap missing, ...
        logger.warning("Could not render subscriptions map: %s", exc)
        await update.effective_message.reply_text(
            caption + "\n\n<i>Zemljevida trenutno ni bilo mogoče narisati.</i>",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return
    try:
        await update.effective_message.reply_photo(
            photo=open(path, "rb"), caption=caption, parse_mode="HTML", reply_markup=markup
        )
    except TelegramError as exc:
        logger.warning("Could not send subscriptions map: %s", exc)
        await update.effective_message.reply_text(
            caption + "\n\n<i>Zemljevida trenutno ni bilo mogoče poslati.</i>",
            parse_mode="HTML",
            reply_markup=markup,
        )


async def _ask_delete(update, context, sub_id, from_callback):
    """Removal always asks for confirmation, naming the location."""
    chat_id = update.effective_chat.id
    row = await asyncio.to_thread(get_subscription, chat_id, sub_id)
    if not row:
        await update.effective_message.reply_text("Take lokacije ni na vašem seznamu.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Da, odstrani", callback_data=f"delok:{sub_id}"),
        InlineKeyboardButton("↩️ Ne", callback_data="delno"),
    ]])
    rows = await asyncio.to_thread(list_subscriptions, chat_id)
    index = next((i for i, r in enumerate(rows) if r["id"] == sub_id), 0)
    question = _remove_question(row, index)
    await _answer(update, question, keyboard, from_callback=from_callback)


def _remove_question(row, index=0):
    """Confirm-removal text: label, the row's own marker and the area it covers."""
    return (
        f"Ali res želite odstraniti <b>{html.escape(row['label'])}</b> "
        f"({kind_emoji(row, index)} {describe_subscription(row)})?\n"
        "Obvestil za to območje ne boste več prejemali."
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return await _refuse_group(update, context)
    if not context.args:
        await update.effective_message.reply_text("Uporaba: /remove <številka> (glejte /list)")
        return
    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Številka ni veljavna. Glejte /list.")
        return
    await _ask_delete(update, context, sub_id, from_callback=False)


async def deleted_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        sub_id = int(query.data.split(":", 1)[1])
    except ValueError:
        return
    await _ask_delete(update, context, sub_id, from_callback=True)


async def confirmed_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        sub_id = int(query.data.split(":", 1)[1])
    except ValueError:
        return
    chat_id = update.effective_chat.id
    row = await asyncio.to_thread(get_subscription, chat_id, sub_id)
    name = html.escape(row["label"]) if row else "lokacija"
    removed = await asyncio.to_thread(remove_subscription, chat_id, sub_id)
    text = (
        f"🗑 Odstranjeno: <b>{name}</b>." if removed
        else f"<b>{name}</b> je že bila odstranjena."
    )
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except TelegramError:
        await context.bot.send_message(chat_id, text, parse_mode="HTML")


async def cancelled_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("V redu, obdržano.")
    try:
        await query.edit_message_text("V redu, <b>lokacija je obdržana</b>.", parse_mode="HTML")
    except TelegramError:
        pass


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return await _refuse_group(update, context)
    removed = await asyncio.to_thread(remove_all_subscriptions, update.effective_chat.id)
    await update.effective_message.reply_text(
        f"Počiščeno: {removed} lokacij. Obvestil ne boste več prejemali." if removed
        else "Nimate shranjenih lokacij."
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _clear_pending(context)
    await update.effective_message.reply_text("Preklicano.")
    return ConversationHandler.END


def build_handlers():
    """Handlers to register on the application."""
    # We mix MessageHandler and CallbackQueryHandler in one conversation, so
    # per_message must stay False; PTB warns about that combination every start.
    warnings.filterwarnings("ignore", message=r"If 'per_message=False'",
                            category=PTBUserWarning)
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("track", cmd_track, filters.ChatType.PRIVATE),
            CommandHandler("track", _refuse_group, filters.ChatType.GROUPS),
            CommandHandler("topics", cmd_topics, filters.ChatType.PRIVATE),
            CommandHandler("topics", _refuse_group, filters.ChatType.GROUPS),
        ],
        states={
            PLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_place)],
            PICK: [CallbackQueryHandler(picked_candidate, pattern=r"^cand:")],
            RADIUS: [
                CallbackQueryHandler(picked_radius, pattern=r"^rad:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_radius),
            ],
            RADIUS_CUSTOM: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_radius)],
            NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_name),
                CallbackQueryHandler(name_from_button, pattern=r"^name:"),
            ],
            TOPICS: [CallbackQueryHandler(picked_topic, pattern=r"^top:")],
            TOPIC_PICK: [
                CallbackQueryHandler(picked_topic_subscription, pattern=r"^tsel:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_topic_subscription),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=True,
    )
    return [
        conversation,
        CommandHandler("start", cmd_start),
        CommandHandler("help", cmd_help),
        CommandHandler("list", cmd_list),
        CommandHandler("remove", cmd_remove),
        CommandHandler("stop", cmd_stop),
        # must sit before the ^del: handler: "delok:" and "delno" share the prefix
        CallbackQueryHandler(confirmed_delete, pattern=r"^delok:"),
        CallbackQueryHandler(cancelled_delete, pattern=r"^delno$"),
        CallbackQueryHandler(deleted_subscription, pattern=r"^del:"),
    ]


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #

def format_match_note(matches):
    """Footer text listing why the user got this incident."""
    parts = []
    for label, distance, radius in matches[:MAX_FORMATTED_MATCHES]:
        if radius:
            parts.append(f"{label} ({distance:.1f} km)")
        else:
            parts.append(f"{label} (v občini)")
    return "📍 " + " · ".join(parts)


async def deliver(bot, chat_id, matches, caption, send_photo, send_text):
    """Send one incident to one subscriber, degrading to text when needed."""
    note = format_match_note(matches)
    text = f"{caption}\n\n<i>{note}</i>"
    try:
        if caption and await send_photo(bot, chat_id, text):
            return "photo"
    except TelegramError as exc:
        logger.warning("Photo delivery to %s failed (%s); trying text.", chat_id, exc)
    try:
        await send_text(bot, chat_id, text)
        return "text"
    except Forbidden:
        logger.info("Chat %s blocked the bot; disabling its subscriptions.", chat_id)
        disable_chat(chat_id)
        return None
    except RetryAfter as exc:
        await asyncio.sleep(getattr(exc, "retry_after", 5))
        try:
            await send_text(bot, chat_id, text)
            return "text"
        except TelegramError as exc2:
            logger.warning("Retry to %s failed: %s", chat_id, exc2)
            return None
    except TelegramError as exc:
        logger.warning("Delivery to %s failed: %s", chat_id, exc)
        return None
