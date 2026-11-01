"""Per-user radius subscriptions for SPIN112.

A user opens a private chat with the bot, runs /track, types a city or village
name and picks a radius.  Every new incident whose coordinates fall inside that
circle is then delivered to that chat as a private message (with the same map
image the group gets).

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
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_UA = "SPIN112-bot/1.0 (SOS112 incident bot; +https://github.com/StellarStoic/SPIN112)"
NOMINATIM_MIN_INTERVAL = 1.1  # Nominatim asks for at most 1 request/second
MAX_RADIUS_KM = 100.0
MIN_RADIUS_KM = 1.0
MAX_FORMATTED_MATCHES = 5
DELIVERY_RETENTION_DAYS = 60

_last_nominatim_call = 0.0

# conversation states
PLACE, PICK, RADIUS, RADIUS_CUSTOM = range(4)

RADIUS_PRESETS = (5, 10, 25, 50)


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
                enabled INTEGER NOT NULL DEFAULT 1
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
        conn.commit()
    logger.info("Radius subscriptions ready (%s).", DB_PATH)


def _build_ob_index():
    """Map upper-cased municipality name -> rough centroid (vertex average)."""
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
            OB_INDEX[name.strip().upper()] = (lat_sum / len(ring), lon_sum / len(ring))
        except Exception as exc:  # malformed geometry must not break startup
            logger.debug("Skipping municipality for geocoding: %s", exc)
    logger.info("Municipality geocoding index holds %d entries.", len(OB_INDEX))


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres."""
    lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    r = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def add_subscription(chat_id, label, lat, lon, radius_km):
    """Insert or update one location for one chat. Returns the row id."""
    radius_km = max(MIN_RADIUS_KM, min(MAX_RADIUS_KM, float(radius_km)))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO subscriptions (chat_id, label, lat, lon, radius_km, created_at, enabled)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id, label) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                radius_km = excluded.radius_km,
                created_at = excluded.created_at,
                enabled = 1
            """,
            (int(chat_id), label, float(lat), float(lon), radius_km, now),
        )
        conn.commit()
        row_id = cur.lastrowid
    logger.info("Subscription saved: chat %s -> %s (%.0f km).", chat_id, label, radius_km)
    return row_id


def list_subscriptions(chat_id):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, label, lat, lon, radius_km FROM subscriptions "
            "WHERE chat_id = ? AND enabled = 1 ORDER BY id",
            (int(chat_id),),
        ).fetchall()
    return [dict(row) for row in rows]


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
    """Return {chat_id: [(label, distance_km, radius_km), ...]} for one incident."""
    lat, lon = float(lat), float(lon)
    matches = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, label, lat, lon, radius_km FROM subscriptions WHERE enabled = 1"
        ).fetchall()
    for row in rows:
        distance = haversine_km(lat, lon, row["lat"], row["lon"])
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
    """Offline fallback: match municipalities from OB.geojson."""
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
            {"label": f"Občina {name.title()} (središče občine)", "lat": lat, "lon": lon, "source": "ob"}
        )
    return results


def geocode(query):
    """Return up to five candidates: [{'label', 'lat', 'lon', 'source'}]."""
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
    if not candidates:
        candidates = geocode_municipalities(query)
    if candidates:
        _cache_put(query, candidates)
    return candidates


# --------------------------------------------------------------------------- #
# Telegram conversation
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "<b>SPIN112 – obveščanje po lokaciji</b>\n\n"
    "Nastavite mesta ali vasi, ki vas zanimajo, in polmer v kilometrih. "
    "Vsako novo intervencijo znotraj tega obsega vam pošljem tukaj, v zasebnem klepetu.\n\n"
    "/track – dodaj lokacijo\n"
    "/list – prikaži in uredi svoje lokacije\n"
    "/remove &lt;številka&gt; – odstrani lokacijo\n"
    "/stop – izbriši vse lokacije\n"
    "/cancel – prekini trenutni korak\n\n"
    "Primer: /track Ljubljana"
)


def _private_only(update):
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def _refuse_group(update, context):
    await update.effective_message.reply_text(
        "Ta ukaz deluje samo v zasebnem klepetu z botom. Pišite mi v zasebno sporočilo."
    )


def _radius_keyboard():
    buttons = [
        InlineKeyboardButton(f"{km} km", callback_data=f"rad:{km}") for km in RADIUS_PRESETS
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:], [InlineKeyboardButton("✏️ drugo", callback_data="rad:custom")]])


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


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        await _refuse_group(update, context)
        return ConversationHandler.END
    query = " ".join(context.args or []).strip()
    if not query:
        await update.effective_message.reply_text(
            "Katero mesto ali vas želite spremljati?\n"
            "Pošljite ime, npr. <i>Ljubljana</i>, <i>Videm pri Ptuju</i>, <i>Bohinjska Bistrica</i>.\n"
            "Za preklic: /cancel",
            parse_mode="HTML",
        )
        return PLACE
    return await _show_candidates(update, context, query)


async def _show_candidates(update, context, query):
    context.user_data["pending_query"] = query
    candidates = await asyncio.to_thread(geocode, query)
    if not candidates:
        await update.effective_message.reply_text(
            f"Za »{html.escape(query)}« nisem našel nobenega kraja v Sloveniji.\n"
            "Poskusite z drugačnim imenom ali dodajte občino (npr. »Videm, Ptuj«)."
        )
        return PLACE
    if len(candidates) == 1:
        context.user_data["candidate"] = candidates[0]
        await update.effective_message.reply_text(
            f"Najdeno: <b>{html.escape(candidates[0]['label'])}</b>\n"
            f"V katerem obsegu naj spremljam to lokacijo?",
            parse_mode="HTML",
            reply_markup=_radius_keyboard(),
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
    return await _show_candidates(update, context, query)


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
        reply_markup=_radius_keyboard(),
    )
    return RADIUS


async def picked_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    value = query.data.split(":", 1)[1]
    if value == "custom":
        await query.edit_message_text(
            f"Vpišite polmer v kilometrih (med {int(MIN_RADIUS_KM)} in {int(MAX_RADIUS_KM)}), npr. 15."
        )
        return RADIUS_CUSTOM
    try:
        radius_km = float(value)
    except ValueError:
        await query.edit_message_text("Napačna vrednost. Začnite znova z /track.")
        return ConversationHandler.END
    return await _save(update, context, radius_km, from_callback=True)


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
            f"Polmer mora biti med {int(MIN_RADIUS_KM)} in {int(MAX_RADIUS_KM)} km."
        )
        return RADIUS_CUSTOM
    return await _save(update, context, radius_km, from_callback=False)


async def _save(update, context, radius_km, from_callback):
    candidate = context.user_data.get("candidate")
    if not candidate:
        text = "Nekaj je šlo narobe. Začnite znova z /track."
        if from_callback:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    await asyncio.to_thread(
        add_subscription,
        chat_id,
        candidate["label"],
        candidate["lat"],
        candidate["lon"],
        radius_km,
    )
    text = (
        f"✅ Dodano: <b>{html.escape(candidate['label'])}</b>, polmer {radius_km:g} km.\n"
        "Nove intervencije v tem obsegu vam pošljem tukaj.\n"
        "Seznam uredite z /list."
    )
    if from_callback:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML")
    context.user_data.pop("candidate", None)
    context.user_data.pop("candidates", None)
    context.user_data.pop("pending_query", None)
    return ConversationHandler.END


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return await _refuse_group(update, context)
    rows = await asyncio.to_thread(list_subscriptions, update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text(
            "Nimate shranjenih lokacij. Dodajte jih s /track."
        )
        return
    lines = ["<b>Vaše lokacije:</b>\n"]
    for row in rows:
        lines.append(f"<code>{row['id']}</code> · {html.escape(row['label'])} · {row['radius_km']:g} km")
    keyboard = [
        [InlineKeyboardButton(f"❌ {row['label'][:40]}", callback_data=f"del:{row['id']}")]
        for row in rows
    ]
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
    removed = await asyncio.to_thread(remove_subscription, update.effective_chat.id, sub_id)
    await update.effective_message.reply_text(
        "Lokacija odstranjena." if removed else "Take lokacije ni na vašem seznamu."
    )


async def deleted_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        sub_id = int(query.data.split(":", 1)[1])
    except ValueError:
        return
    removed = await asyncio.to_thread(remove_subscription, update.effective_chat.id, sub_id)
    await query.edit_message_text(
        "Lokacija odstranjena." if removed else "Lokacija je že odstranjena."
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _private_only(update):
        return await _refuse_group(update, context)
    removed = await asyncio.to_thread(remove_all_subscriptions, update.effective_chat.id)
    await update.effective_message.reply_text(
        f"Počiščeno: {removed} lokacij. Obvestil ne boste več prejemali." if removed
        else "Nimate shranjenih lokacij."
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ("candidate", "candidates", "pending_query"):
        context.user_data.pop(key, None)
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
        CallbackQueryHandler(deleted_subscription, pattern=r"^del:"),
    ]


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #

def format_match_note(matches):
    """Footer text listing why the user got this incident."""
    parts = [f"{label} ({distance:.1f} km)" for label, distance, _ in matches[:MAX_FORMATTED_MATCHES]]
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
