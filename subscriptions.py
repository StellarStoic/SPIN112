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
PLACE, PICK, RADIUS, RADIUS_CUSTOM, NAME = range(5)

RADIUS_PRESETS = (5, 10, 25, 50)

# Slovenia bounding box, used to sanity-check hand-typed coordinates
SI_LAT_MIN, SI_LAT_MAX = 45.35, 46.95
SI_LON_MIN, SI_LON_MAX = 13.20, 16.80

MAX_LABEL_LEN = 60

# Slovenia-focused tile styles for the overview map
MAP_STYLE_URLS = {
    "topo": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    "default": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "light": "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
}
MAP_COLORS = (
    "#e01b24", "#1c71d8", "#2ec27e", "#e66100",
    "#813d9c", "#0f8b8d", "#c01c8a", "#8a6d00",
)
# same order as MAP_COLORS, used in the /list caption so the legend matches the map
MAP_EMOJI = ("🔴", "🔵", "🟢", "🟠", "🟣", "🟦", "🟪", "🟤")

MAX_SUBSCRIPTIONS = 15


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
                ob_name TEXT
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


def add_subscription(chat_id, label, lat, lon, radius_km, kind="circle", ob_name=None):
    """Insert or update one location for one chat. Returns the row id.

    ``kind`` is ``'circle'`` (a point plus radius) or ``'obcina'`` (a whole
    municipality, matched by its polygon; radius is stored as 0).
    """
    kind = "obcina" if kind == "obcina" else "circle"
    if kind == "obcina":
        radius_km = 0.0
    else:
        radius_km = max(MIN_RADIUS_KM, min(MAX_RADIUS_KM, float(radius_km)))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO subscriptions
                (chat_id, label, lat, lon, radius_km, created_at, enabled, kind, ob_name)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id, label) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                radius_km = excluded.radius_km,
                created_at = excluded.created_at,
                enabled = 1,
                kind = excluded.kind,
                ob_name = excluded.ob_name
            """,
            (int(chat_id), label, float(lat), float(lon), radius_km, now, kind, ob_name),
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
        "občina" if kind == "obcina" else "%.0f km" % radius_km,
    )
    return row_id


_COLUMNS = "id, chat_id, label, lat, lon, radius_km, kind, ob_name"


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
    """Human readable one-liner, e.g. '10 km' or 'cela občina Ptuj'."""
    if (row.get("kind") or "circle") == "obcina":
        name = (row.get("ob_name") or "").strip()
        return f"cela občina {name}" if name else "cela občina"
    return f"{(row.get('radius_km') or 0):g} km"


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


def match_subscribers(lat, lon):
    """Return {chat_id: [(label, distance_km, radius_km), ...]} for one incident.

    Circle subscriptions match by distance; občina subscriptions match by their
    polygon, so an incident anywhere inside the municipality counts.
    """
    lat, lon = float(lat), float(lon)
    matches = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, label, lat, lon, radius_km, kind, ob_name "
            "FROM subscriptions WHERE enabled = 1"
        ).fetchall()
    for row in rows:
        distance = haversine_km(lat, lon, row["lat"], row["lon"])
        if (row["kind"] or "circle") == "obcina":
            if point_in_obcina(lat, lon, row["ob_name"]):
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


def render_subscriptions_map(rows, path, width=1000, height=760):
    """Draw every followed location (radius circles and municipality areas) on one map."""
    from staticmap import CircleMarker, Line, Polygon, StaticMap

    if not rows:
        raise ValueError("nothing to draw")
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
        else:
            ring = circle_ring(row["lat"], row["lon"], row.get("radius_km") or MIN_RADIUS_KM)
            overview.add_polygon(Polygon(ring, fill, outline))
            overview.add_line(Line(ring + [ring[0]], outline, 3))
        overview.add_marker(CircleMarker((row["lon"], row["lat"]), outline, 7))
    overview.render().save(path)
    return path


HELP_TEXT = (
    "<b>SPIN112 – obveščanje po lokaciji</b>\n\n"
    "Nastavite mesta, vasi ali cele občine, ki vas zanimajo, in vsako novo "
    "intervencijo znotraj tega območja vam pošljem tukaj, v zasebnem klepetu – "
    "z zemljevidom vred. Skupinska sporočila v kanalu ostanejo nespremenjena; "
    "to je vaš osebni filter.\n\n"
    "<b>Ukazi</b>\n"
    "<b>/track &lt;kraj&gt;</b> – dodaj lokacijo po imenu. Bot poišče kraj, vi "
    "izberete pravega in nato polmer.\n"
    "<b>/track &lt;širina&gt; &lt;dolžina&gt; [polmer]</b> – dodaj točno točko po "
    "koordinatah, npr. <code>/track 46.2397 14.3567 5</code> (širina, dolžina, "
    "polmer v km). Polmer lahko izpustite in bot vas bo vprašal zanj. "
    "Vrstni red je vedno <i>širina (lat) dolžina (lon) polmer</i>; decimalna "
    "pika ali vejica sta enako veljavni.\n"
    "<b>/track Občina Ptuj</b> – pri občinah bot ponudi tudi »cela občina«, "
    "brez polmera: obvestilo dobiš, kadar je intervencija kjerkoli znotraj meje "
    "občine.\n"
    "<b>/list</b> – prikaže vse vaše lokacije <i>na zemljevidu</i>, z narisanimi "
    "polmeri in mejami občin, ter gumbe za brisanje.\n"
    "<b>/remove &lt;številka&gt;</b> – odstrani lokacijo s tega seznama; bot "
    "vedno še enkrat vpraša za potrditev.\n"
    "<b>/stop</b> – izbriše vse vaše lokacije (z potrditvijo).\n"
    "<b>/cancel</b> – prekini trenutni korak. /help – ta pomoč.\n\n"
    "<b>Imena lokacij</b>\n"
    "Vsaki lokaciji lahko daste svoje ime (»Doma«, »Služba«, »Vikend«). Ko "
    "vnesete koordinate, bot vedno vpraša, kako naj jo imenuje – ime se pojavi "
    "v /list in v obvestilih, da takoj veste, zakaj ste sporočilo dobili.\n\n"
    "<b>Primeri</b>\n"
    "<code>/track Ljubljana</code>\n"
    "<code>/track Videm pri Ptuju</code>\n"
    "<code>/track 46.2397 14.3567 10</code>\n"
    "<code>/track 46.2397 14.3567</code> – polmer vpraša naslednji korak\n\n"
    "Obseg: polmer od 1 do 100 km, največ 15 lokacij na klepet."
)


def _private_only(update):
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def _refuse_group(update, context):
    await update.effective_message.reply_text(
        "Ta ukaz deluje samo v zasebnem klepetu z botom. Pišite mi v zasebno sporočilo."
    )


def _radius_keyboard(ob_name=None):
    """Radius presets; municipalities get an extra "whole municipality" row."""
    buttons = [
        InlineKeyboardButton(f"{km} km", callback_data=f"rad:{km}") for km in RADIUS_PRESETS
    ]
    rows = [buttons[:2], buttons[2:], [InlineKeyboardButton("✏️ drugo", callback_data="rad:custom")]]
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


def _clear_pending(context):
    for key in ("pending", "candidate", "candidates", "pending_query"):
        context.user_data.pop(key, None)


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        await _refuse_group(update, context)
        return ConversationHandler.END
    query = " ".join(context.args or []).strip()
    if not query:
        await update.effective_message.reply_text(
            "Katero mesto, vas ali občino želite spremljati?\n"
            "Pošljite ime (<i>Ljubljana</i>, <i>Videm pri Ptuju</i>) ali koordinate "
            "(<code>46.2397 14.3567 5</code> – širina, dolžina, polmer v km).\n"
            "Za preklic: /cancel",
            parse_mode="HTML",
        )
        return PLACE
    return await _start_from_text(update, context, query, from_callback=False)


async def _start_from_text(update, context, text, from_callback=False):
    """Route user input: a coordinate triple goes to the name prompt, else geocode."""
    parsed = parse_coordinates(text)
    if parsed is None:
        return await _show_candidates(update, context, text)
    outcome = await _use_coordinates(update, context, parsed, from_callback)
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
            "Vrstni red je <b>širina (lat) dolžina (lon) polmer</b> – npr. "
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
            f"Polmer mora biti med {MIN_RADIUS_KM:g} in {MAX_RADIUS_KM:g} km. "
            f"Vpišite polmer za točko {label} (npr. 10).",
            from_callback=from_callback,
        )
        return "radius"
    context.user_data["pending"]["radius_km"] = radius
    await _ask_name(update, context, from_callback)
    return "name"


async def _ask_name(update, context, from_callback=False):
    """Always ask what the new location should be called."""
    pending = context.user_data.get("pending") or {}
    suggestion = (pending.get("label") or "")[:MAX_LABEL_LEN]
    details = []
    if pending.get("label"):
        details.append(f"📍 <code>{html.escape(pending['label'])}</code>")
    if pending.get("radius_km"):
        details.append(f"polmer {pending['radius_km']:g} km")
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
    return await _commit(update, context, raw, from_callback=False)


async def name_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending") or {}
    return await _commit(update, context, pending.get("label", ""), from_callback=True)


async def _commit(update, context, label, from_callback=False):
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
    await asyncio.to_thread(
        add_subscription, chat_id, label, pending["lat"], pending["lon"],
        radius_km, kind, pending.get("ob_name"),
    )
    if kind == "obcina":
        scope = f"cela občina <b>{html.escape(pending.get('ob_name') or '')}</b>"
    else:
        scope = f"polmer {radius_km:g} km"
    verb = "Posodobljeno" if same_label else "Dodano"
    await _answer(
        update,
        f"✅ {verb}: <b>{html.escape(label)}</b> – {scope}.\n"
        f"📍 <code>{pending['lat']:.4f}, {pending['lon']:.4f}</code>\n"
        "Nove intervencije v tem območju vam pošljem tukaj. Vse lokacije in "
        "zemljevid: /list.",
        from_callback=from_callback,
    )
    _clear_pending(context)
    return ConversationHandler.END


async def _show_candidates(update, context, query):
    context.user_data["pending_query"] = query
    candidates = await asyncio.to_thread(geocode, query)
    if not candidates:
        await update.effective_message.reply_text(
            f"Za »{html.escape(query)}« nisem našel nobenega kraja v Sloveniji.\n"
            "Poskusite z drugačnim imenom ali vnesite koordinate "
            "(<code>46.2397 14.3567 5</code>)."
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
            f"Vpišite polmer v kilometrih (med {MIN_RADIUS_KM:g} in {MAX_RADIUS_KM:g}), npr. 15."
        )
        return RADIUS_CUSTOM
    candidate = context.user_data.get("candidate") or {}
    if value == "obc":
        ob_name = candidate.get("ob_name")
        if not ob_name:
            await query.edit_message_text("Za to lokacijo ne poznam meje občine. Izberite polmer.")
            return RADIUS
        context.user_data["pending"] = {
            "label": f"Občina {ob_name}",
            "lat": candidate["lat"],
            "lon": candidate["lon"],
            "kind": "obcina",
            "ob_name": ob_name,
            "radius_km": 0.0,
        }
        return await _commit(update, context, f"Občina {ob_name}", from_callback=True)
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
            "To ni število. Vpišite polmer v kilometrih, npr. 15."
        )
        return RADIUS_CUSTOM
    if not (MIN_RADIUS_KM <= radius_km <= MAX_RADIUS_KM):
        await update.effective_message.reply_text(
            f"Polmer mora biti med {MIN_RADIUS_KM:g} in {MAX_RADIUS_KM:g} km."
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
    return await _commit(update, context, candidate["label"], from_callback=from_callback)


def _list_caption(rows):
    lines = [f"<b>Vaše lokacije ({len(rows)}):</b>"]
    for index, row in enumerate(rows):
        scope = describe_subscription(row)
        obcina = (row.get("kind") or "circle") == "obcina"
        coords = "" if obcina else f" · {row['lat']:.4f}, {row['lon']:.4f}"
        lines.append(
            f"{_emoji(index)} <code>{row['id']}</code> · <b>{html.escape(row['label'])}</b>"
            f" · {scope}{coords}"
        )
    lines.append("")
    lines.append("Barve na zemljevidu se ujemajo s krogci zgoraj. "
                 "Odstranitev: /remove &lt;številka&gt; ali gumb spodaj.")
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
    question = (
        f"Ali res želite odstraniti <b>{html.escape(row['label'])}</b> "
        f"({describe_subscription(row)})?\n"
        "Obvestil za to območje ne boste več prejemali."
    )
    await _answer(update, question, keyboard, from_callback=from_callback)


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
