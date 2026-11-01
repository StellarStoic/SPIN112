import os
import re
from dotenv import load_dotenv  # Import the dotenv library
import requests
import xml.etree.ElementTree as ET
import asyncio  # asynchronous sleep and operations
from telegram import Bot, BotCommand, InputFile, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackContext
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest, TelegramError, Forbidden
import logging
import time
from datetime import datetime
import json
from staticmap import StaticMap, CircleMarker, Polygon, Line
from shapely.geometry import shape, Point  # shapely for geojson region matching
from shapely.geometry import Polygon as ShapelyPolygon, Point as ShapelyPoint  # Import Shapely's Polygon and Point
from PIL import Image, ImageEnhance
from io import BytesIO
import subscriptions  # per-user radius subscriptions (private chat)

# Load environment variables from .env file
load_dotenv()

# Configure logging
_file_handler = logging.FileHandler("SPIN112_bot_errors.log", encoding="utf-8")
_file_handler.setLevel(logging.WARNING)  # file keeps only real problems
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[_file_handler, logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Retrieve variables from .env file
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_GROUP_ID = os.getenv('TELEGRAM_GROUP_ID')

# Maximum number of reports to store in the JSON files
MAX_STORED_REPORTS = 1000

# Verify if the variables are loaded correctly
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_GROUP_ID:
    logger.critical("Telegram bot token or group ID is missing. Please check your .env file.")
    exit(1)
    
# Define a dictionary to map categories to Telegram topic IDs
topics = {
    # All
    "All": None,  # Set the value to None to indicate the main group thread

    # Region
    "POMURSKA": 3,
    "PODRAVSKA": 4,
    "KOROŠKA": 6,
    "SAVINJSKA": 8,
    "ZASAVSKA": 10,
    "POSAVSKA": 12,
    "OSREDNJESLOVENSKA": 16,
    "JUGOVZHODNA SLOVENIJA": 14,
    "GORENJSKA": 18,
    "PRIMORSKO-NOTRANJSKA": 20,
    "GORIŠKA": 23,
    "OBALNO-KRAŠKA": 25,

    # Intervention type
    "Tehnična in druga pomoč": 29,
    "Tehnična in druga pomoč - jama": 245,
    "Tehnična in druga pomoč - plaz": 246,
    "Jedrska ali radiološka nevarnost": 225,
    "Epidemije": 220,
    "Motnje, omejitve in prekinitve oskrbe": 27,
    "Najdbe NUS": 240,
    "Onesnaženje, nesreče z nevarnimi snovmi": 242,
    "Požar, eksplozija": 243,
    "Prometna nesreča": 244,
    "Prevoz pitne vode": 247,
    "Lažne in nepotrebne intervencije": 248,

    # Word checking in dogodekNaziv
    "Gore": 461,
    "Športne aktivnosti": 462,
    "Nevarne snovi": 463,
    
    # Večji obseg
    "Večji obseg": 1404,  # New topic ID for Večji obseg

    # Unverified first reports ("Prva prijava-Nepreverjen dogodek")
    "Prva prijava-Nepreverjen dogodek": 3125,
}

# --- Topic id overrides ------------------------------------------------------
# When a topic is deleted in Telegram (its message_thread_id stops existing)
# the bot can recreate it under the same name, but the new id differs from the
# hardcoded one above. Recreated ids are persisted here so a restart keeps
# posting into the same topic instead of failing again.
TOPIC_IDS_FILE = 'topic_ids.json'
_topic_id_overrides = {}


def topic_name_for_id(topic_id):
    """Return the topic name that maps to ``topic_id`` (None for the main chat)."""
    if topic_id is None:
        return None
    for _name, _tid in topics.items():
        if _tid == topic_id:
            return _name
    return None


def load_topic_overrides():
    """Apply ids stored in TOPIC_IDS_FILE on top of the hardcoded map."""
    global _topic_id_overrides
    _topic_id_overrides = {}
    try:
        with open(TOPIC_IDS_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return
    except Exception as exc:  # corrupt state file must not stop the bot
        logger.error(f"Could not read {TOPIC_IDS_FILE}: {exc}")
        return
    if not isinstance(data, dict):
        logger.error(f"{TOPIC_IDS_FILE} does not contain an object; ignoring it.")
        return
    for name, tid in data.items():
        if name in topics and isinstance(tid, int) and tid > 0:
            topics[name] = tid
            _topic_id_overrides[name] = tid
    if _topic_id_overrides:
        logger.warning(f"Topic id overrides applied from {TOPIC_IDS_FILE}: {_topic_id_overrides}")


def save_topic_overrides():
    try:
        with open(TOPIC_IDS_FILE, 'w', encoding='utf-8') as fh:
            json.dump(_topic_id_overrides, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error(f"Could not write {TOPIC_IDS_FILE}: {exc}")


async def ensure_topic_thread(bot, chat_id, topic_id):
    """Recreate a deleted topic and return its new message_thread_id.

    Needs the bot's "Manage topics" admin right. Returns None when the topic
    cannot be recreated (unknown id, missing right, Telegram error).
    """
    name = topic_name_for_id(topic_id)
    if name is None:
        logger.error(f"Thread id {topic_id} is not in the topic map; cannot recreate it.")
        return None
    # Cheap existence probe: never create a second topic when the original is
    # still there and the send failed for some other reason.
    probe = getattr(bot, 'edit_forum_topic', None)
    if probe is not None:
        try:
            await probe(chat_id=chat_id, message_thread_id=topic_id, name=name)
            logger.warning(f"Topic '{name}' (id {topic_id}) still exists; not recreating it.")
            return None
        except TelegramError as exc:
            if 'message thread not found' not in str(exc).lower():
                logger.warning(f"Topic '{name}' (id {topic_id}) probe failed: {exc}")
    try:
        created = await bot.create_forum_topic(chat_id=chat_id, name=name)
    except TelegramError as exc:
        logger.error(f"Topic '{name}' (id {topic_id}) is gone and could not be recreated: {exc}")
        return None
    new_id = getattr(created, 'message_thread_id', None)
    if not new_id:
        logger.error(f"Recreate of topic '{name}' returned no message_thread_id.")
        return None
    topics[name] = new_id
    _topic_id_overrides[name] = new_id
    save_topic_overrides()
    logger.warning(
        f"Recreated missing topic '{name}': id {topic_id} -> {new_id} (saved to {TOPIC_IDS_FILE})"
    )
    return new_id


# Keyword mappings for topics
keywords_map = {
    "Gore": ["gorah", "gore", "Triglav", "alpe", "Karavanke", "sestopu", "zdrs"],
    "Športne aktivnosti": ["adrenalinske", "adrenalinskih", "športnih", "šport", "športu", "rekreativnih"],
    "Nevarne snovi": ["snovmi", "snovi", "nevarne", "nevarnimi", "strupene", "strupenimi"]
}

# Defining the RSS feed URL and incident details URL base
rss_feed_url = "https://spin3.sos112.si/api/javno/ODRSS/false" # Samo preverjene intervencije
# rss_feed_url = "https://spin3.sos112.si/api/javno/ODRSS/true" # Vse intervencije vključno z nepreverjenimi

incident_details_url_base = "https://spin3.sos112.si/api/javno/lokacija/"
vecji_obseg_url = "https://spin3.sos112.si/javno/assets/data/vecjiObseg.json"

# File path for storing posted incidents
posted_incidents_file = 'posted_incidents.json' # ID's only
posted_vecji_obseg_file = 'posted_vecjiObseg.json'
posted_nepreverjeni_incidents_file = 'posted_nepreverjeni_incidents.json'  # first reports already announced
posted_nepreverjeni_messages_file = 'posted_nepreverjeni_messages.json'    # their message ids, for later deletion

# GeoJSON file path for "regije" data and "občine" data
geojson_file = "SR.geojson"
ob_geojson_file = "OB.geojson"

# Global variables to keep track of incidents
fetched_incidents = set()
posted_vecji_obseg = set()
posted_incidents = {}


# Dictionary to map English day names to custom names
custom_day_names = {
    'Mon': 'Pon',
    'Tue': 'Tor',
    'Wed': 'Sre',
    'Thu': 'Čet',
    'Fri': 'Pet',
    'Sat': 'Sob',
    'Sun': 'Ned'
}

# Function to format the timestamp into the desired format and replace day names
def format_timestamp(timestamp):
    try:
        # Parse the original timestamp into a datetime object
        date_object = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S')
        # Format the datetime object to the desired format (e.g., Tue, 01 Oct 2024 10:25:37)
        formatted_time = date_object.strftime('%a, %d %b %Y %H:%M:%S')

        # Replace the English day names with the custom names using the dictionary
        for eng_day, custom_day in custom_day_names.items():
            formatted_time = formatted_time.replace(eng_day, custom_day)

        return formatted_time
    except ValueError:
        # Return the original timestamp if parsing fails
        return timestamp

# Function to format a timestamp to remove the time portion and replace day names
def format_date_without_time(timestamp):
    """
    Format a timestamp to remove the time portion and return only the date.
    Replace English day names with custom names.
    """
    try:
        # Parse the original timestamp into a datetime object
        date_object = datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%S')
        # Format the datetime object to the desired format (e.g., 'Tue, 04 Oct 2024')
        formatted_date = date_object.strftime('%a, %d %b %Y')  # Only include the date part
        
        # Replace the English day names with the custom names using the dictionary
        for eng_day, custom_day in custom_day_names.items():
            formatted_date = formatted_date.replace(eng_day, custom_day)

        return formatted_date
    except ValueError:
        # Return the original timestamp if parsing fails
        return timestamp


# keywords to emojis
emoji_mapping = {
    "tehnična": ["🔧","🪜"],
    "strupenih": ["☠️"],
    "radioaktivnih": "☢️",
    "plinov": ["💨","🧪"],
    "plini": ["💨", "🧪"],
    "razlitje": ["🫗"],
    "razlitih": ["🫗"],
    "snovi": ["🛢️"],
    "snovmi": ["🛢️"],
    "snov": ["🛢️"],
    "nevarnih": ["⚠️"],
    "nevarnimi": ["⚠️"],
    "požar": ["🔥"],
    "eksplozija": ["💥"],
    "nus": ["⏲️","💣"],
    "prometna": ["🚦"],
    "nesreča": ["🚗", "🚨"],
    "epidemija": ["🦠", "⚕️"],
    "nestanovanjskih": ["🏬"],
    "stanovanjskih": ["🏘️"],
    "industrijskih": ["🏭"],
    "kamnin": ["🪨"],
    "naravi": ["🌳"],
    "gorah": ["🏔️"],
    "zabojnikih": ["🗑️"],
    "gobarjenje": ["🍄"],
    "prometu": ["🚥"],
    "plaz": ["🛝"],
    "adrenalinskih": ["🪂", "🚴", "🪃", "🎿", "🛹", "🛶"],
    "vodi": ["🛟"],
    "živali": ["🐶", "🐱"],
}

def get_emojis_for_keywords(*args):
    """
    Function to get the emojis based on keywords present in the given text fields.
    Uses regular expressions to find exact matches.

    Parameters:
        *args: Multiple text fields (dogodekNaziv, besedilo, intervencijaVrstaNaziv).

    Returns:
        A string of unique emojis based on keyword matches across all text fields.
    """
    matched_emojis = set()  # Use a set to avoid duplicates

    # Iterate over all given text fields
    for text in args:
        # Check each keyword in the text and add corresponding emojis to the set using exact word boundaries
        for keyword, emojis in emoji_mapping.items():
            # Use word boundaries (\b) to match exact words only, ignoring case
            if re.search(rf'\b{re.escape(keyword.lower())}\b', text.lower()):
                matched_emojis.update(emojis if isinstance(emojis, list) else [emojis])

    # Combine matched emojis into a single string
    return ''.join(matched_emojis) if matched_emojis else "."

# Headers for requests
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Accept-Language': 'en-SI,en;q=0.8',
    'DNT': '1',
    'Sec-CH-UA': '"Brave";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
    'Sec-CH-UA-Mobile': '?0',
    'Sec-CH-UA-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-GPC': '1',
    'Pragma': 'no-cache',
    'Cache-Control': 'no-store,no-cache',
}

# Load geojson region data
with open(geojson_file, 'r') as geo_file:
    geojson_data = json.load(geo_file)
    
# Load OB geojson data
with open(ob_geojson_file, 'r') as ob_file:
    ob_geojson_data = json.load(ob_file)

# Per-user radius subscriptions: SQLite store + offline municipality index
subscriptions.init(
    db_path=os.getenv('SUBSCRIPTIONS_DB', 'subscriptions.db'),
    ob_features=ob_geojson_data.get('features', []),
)

# Function to determine the region based on coordinates
def get_region_from_coordinates(lat, lon):
    point = Point(lon, lat)
    for feature in geojson_data['features']:
        polygon = shape(feature['geometry'])
        if polygon.contains(point):
            return feature['properties']['SR_UIME'].upper()
    return None

# START Večji obseg

# Function to get OB region polygon and its centroid, with additional logging and validation
def get_ob_region_and_centroid(obcinaNaziv):
    """
    Get the OB region polygon coordinates and its centroid from the given obcinaNaziv.
    This function returns both the polygon and its centroid if found.
    Handles both Polygon and MultiPolygon cases, and validates the format.
    """
    try:
        for feature in ob_geojson_data['features']:
            # Check if the feature's OB_UIME matches the given obcinaNaziv
            if feature['properties']['OB_UIME'].upper() == obcinaNaziv.upper():
                # Extract the geometry type and coordinates
                geometry_type = feature['geometry']['type']
                geometry_coords = feature['geometry']['coordinates']
                
                # Log the geometry type and coordinates for debugging
                logger.info(f"Geometry type for {obcinaNaziv}: {geometry_type}")
                logger.info(f"Coordinates for {obcinaNaziv}: {geometry_coords}")

                # Handle Polygon type
                if geometry_type == 'Polygon':
                    # GeoJSON Polygons have coordinates structured as: [[ [lon, lat], [lon, lat], ... ]]
                    polygon_coords = geometry_coords[0]
                    shapely_polygon = ShapelyPolygon(polygon_coords)
                    centroid = shapely_polygon.centroid
                    return polygon_coords, centroid
                
                # Handle MultiPolygon type (if needed)
                elif geometry_type == 'MultiPolygon':
                    # GeoJSON MultiPolygons have coordinates structured as: [[[ [lon, lat], [lon, lat], ... ]]]
                    # Use the first polygon's coordinates within the MultiPolygon
                    polygon_coords = geometry_coords[0][0]
                    shapely_polygon = ShapelyPolygon(polygon_coords)
                    centroid = shapely_polygon.centroid
                    return polygon_coords, centroid

                # Handle Point type if the coordinates are only a single point (fallback)
                elif geometry_type == 'Point':
                    point_coords = geometry_coords
                    shapely_point = ShapelyPoint(point_coords[0], point_coords[1])
                    return [point_coords], shapely_point  # Return as a single point in a list

                else:
                    logger.error(f"Unsupported geometry type for {obcinaNaziv}: {geometry_type}")
                    return None, None

        # If no matching region is found
        logger.warning(f"No matching region found for: {obcinaNaziv}")
    except Exception as e:
        logger.error(f"Error in get_ob_region_and_centroid for {obcinaNaziv}: {e}")
    return None, None


# Fetch and parse vecjiObseg.json data
def get_vecji_obseg_data(url):
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch vecjiObseg data: {e}")
        return None

# Function to handle retries for sending messages with a properly defined message parameter
async def retry_send_message(bot, chat_id, text, message_thread_id=None, retries=5):
    """Send an HTML message. Returns the sent Message, or None on failure."""
    healed = False
    for attempt in range(retries):
        try:
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', message_thread_id=message_thread_id)
        except BadRequest as e:
            logger.error(f"Failed to send message: {e}")
            if 'message thread not found' in str(e).lower():
                if not healed:
                    healed = True
                    new_id = await ensure_topic_thread(bot, chat_id, message_thread_id)
                    if new_id:
                        message_thread_id = new_id
                        continue
                logger.error(f"Invalid message thread ID: {message_thread_id}. Skipping this post.")
                break
            if 'chat not found' in str(e).lower() or 'message is too long' in str(e).lower():
                break
            await asyncio.sleep(5)
        except TelegramError as e:
            logger.error(f"Message send attempt {attempt + 1}/{retries} failed: {e}")
            await asyncio.sleep(5)
    return None

def create_static_map_with_polygon(polygon_coordinates, filename='obcina_map.png', zoom=11, line_width=3, map_style='topo', saturation_level=0.7):
    """
    Create a static map image and draw a polygon border representing the region using Line object.
    Adjust the saturation of the map image.

    Parameters:
        polygon_coordinates (list of tuple): List of (lon, lat) tuples representing the polygon vertices.
        filename (str): The output filename for the static map image.
        zoom (int): The zoom level for the static map image.
        line_width (int): The thickness of the polygon border.
        map_style (str): The style of the map. Options: 'default', 'topo', 'light', 'dark'.
        saturation_level (float): The level of saturation to apply (0.0 for grayscale, 1.0 for original).

    Returns:
        None
    """
    # Check if polygon coordinates are provided
    if not polygon_coordinates:
        print("Empty or invalid polygon coordinates provided. Cannot create map.")
        return

    # Map style URL templates
    style_urls = {
        'default': 'http://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'topo': 'http://a.tile.opentopomap.org/{z}/{x}/{y}.png',
        'light': 'http://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
        'dark': 'http://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'
    }

    # Get the URL template based on the selected map style
    url_template = style_urls.get(map_style, style_urls['topo'])

    try:
        # Create the static map with the chosen style
        m = StaticMap(800, 600, url_template=url_template)

        # Create the line object representing the polygon border, with adjustable width
        line = Line(polygon_coordinates, "blue", width=line_width)

        # Add the line to the map (this will be used to draw the polygon border)
        m.add_line(line)

        # Render the map with the line at the specified zoom level
        image = m.render(zoom=zoom)

        # Save the rendered image to a BytesIO object
        image_buffer = BytesIO()
        image.save(image_buffer, format='PNG')
        image_buffer.seek(0)  # Move cursor to the beginning of the BytesIO object

        # Open the image using PIL from the BytesIO object
        pil_image = Image.open(image_buffer)

        # Apply the saturation filter using ImageEnhance
        enhancer = ImageEnhance.Color(pil_image)
        image_enhanced = enhancer.enhance(saturation_level)

        # Save the enhanced image
        image_enhanced.save(filename)
        print(f"Static map with polygon border saved as {filename} with {map_style} style and reduced saturation.")
    except Exception as e:
        print(f"Failed to create static map with polygon border. Error: {e}")
        
# Function to determine the region based on a centroid point and SR.geojson regions
def get_region_from_centroid(centroid):
    """
    Determine the region based on the centroid point using SR.geojson data.
    """
    for feature in geojson_data['features']:
        polygon = shape(feature['geometry'])
        if polygon.contains(centroid):
            return feature['properties']['SR_UIME'].upper()
    return None

# Function to create a shapely polygon from OB coordinates and get its centroid
def get_centroid_of_ob_region(obcinaNaziv):
    """
    Get the centroid of the OB region polygon from the given obcinaNaziv.
    """
    for feature in ob_geojson_data['features']:
        if feature['properties']['OB_UIME'].upper() == obcinaNaziv.upper():
            polygon_coords = feature['geometry']['coordinates'][0][0]
            shapely_polygon = ShapelyPolygon(polygon_coords)
            return shapely_polygon.centroid  # Return the centroid of the polygon
    return None

# Function to post vecjiObseg incidents to the Večji obseg topic with enhanced error handling
async def post_vecji_obseg_incidents(bot, incident):
    try:
        obcinaNaziv = incident.get('obcinaNaziv', 'N/A')
        besedilo = incident['besediloList'][0].get('besedilo', 'N/A')
        datum = incident['besediloList'][0].get('datum', 'N/A')

        # Format the timestamp to exclude time (specific to vecjiObseg)
        formatted_datum = format_date_without_time(datum) if datum != 'N/A' else 'N/A'

        # Get OB region polygon or point coordinates and centroid
        ob_region, centroid = get_ob_region_and_centroid(obcinaNaziv)

        if not ob_region or not centroid:
            logger.warning(f"Region or centroid not found for: {obcinaNaziv}. Skipping map creation.")
            ob_map_text = ""
        else:
            # Determine the region name from centroid coordinates
            region_name = get_region_from_centroid(centroid)
            logger.info(f"Centroid for {obcinaNaziv}: {centroid}. Region: {region_name}")

            # Check if polygon coordinates are valid for plotting
            if ob_region and isinstance(ob_region, list) and all(isinstance(point, list) and len(point) == 2 for point in ob_region):
                # Create and save a static map image with polygon boundaries
                create_static_map_with_polygon(ob_region, filename='obcina_map.png')
                ob_map_text = f"\nMap region: {obcinaNaziv} with boundaries plotted."
            else:
                logger.error(f"Invalid polygon data for {obcinaNaziv}: {ob_region}")
                ob_map_text = f"\nMap region: {obcinaNaziv} (Polygon data unavailable)."

        # Construct the message
        message = (
            f"🚩 <b>Dogodek večjega obsega</b>\n"
            f"<b>{obcinaNaziv}</b>\n"
            f"<i>Datum:</i> {formatted_datum}\n\n"
            f"{besedilo}"
        )

        # Determine the topic ID based on the region name from the centroid
        region_topic_id = topics.get(region_name, topics["Večji obseg"])  # Default to Večji obseg if region not found
        logger.info(f"Posting incident for region: {region_name} in topic ID: {region_topic_id}")

        # Send the message and map to the Region-specific topic
        await retry_send_photo(bot, TELEGRAM_GROUP_ID, 'obcina_map.png', message, message_thread_id=region_topic_id)

        # Send the message and map to the Večji obseg topic
        vecji_obseg_topic_id = topics["Večji obseg"]
        logger.info(f"Posting incident to Večji obseg topic (ID: {vecji_obseg_topic_id})")
        await retry_send_photo(bot, TELEGRAM_GROUP_ID, 'obcina_map.png', message, message_thread_id=vecji_obseg_topic_id)

        # Send the message and map to the All topic (no message_thread_id for main group)
        logger.info("Posting incident to the All topic (main group)")
        await retry_send_photo(bot, TELEGRAM_GROUP_ID, 'obcina_map.png', message)

    except Exception as e:
        logger.error(f"An error occurred while posting vecji obseg incidents: {e}")

        
# Function to read posted vecjiObseg incidents (as full JSON objects)
def read_posted_vecji_obseg(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return json.load(file)  # Load the entire list of incident JSON objects
    except (FileNotFoundError, json.JSONDecodeError):
        return []

# Function to write posted vecjiObseg incidents (as full JSON objects)
def write_posted_vecji_obseg(file_path, incident_list):
    # Keep only the most recent MAX_STORED_REPORTS incidents
    if len(incident_list) > MAX_STORED_REPORTS:
        incident_list = incident_list[-MAX_STORED_REPORTS:]

    # Write the limited list of incidents back to the JSON file
    with open(file_path, 'w', encoding='utf-8') as file:
        json.dump(incident_list, file, ensure_ascii=False, indent=4)
        
# Function to compare if two incidents are the same based on content
def is_duplicate_incident(new_incident, posted_incidents):
    """
    Check if the new incident is a duplicate by comparing it with each posted incident.
    """
    for posted_incident in posted_incidents:
        # Compare the entire JSON object of both incidents
        if new_incident == posted_incident:
            return True
    return False

# Function to fetch and post new vecjiObseg incidents automatically
async def fetch_and_post_vecji_obseg(context: CallbackContext):
    global posted_vecji_obseg
    
    logger.info("Checking for new vecjiObseg incidents...")  # Log the start of the check

    # Load posted incidents from the JSON file
    posted_vecji_obseg = read_posted_vecji_obseg(posted_vecji_obseg_file)

    # Get the vecjiObseg data
    vecji_obseg_data = get_vecji_obseg_data(vecji_obseg_url)
    if not vecji_obseg_data or 'value' not in vecji_obseg_data:
        logger.warning("Failed to fetch or parse vecjiObseg data.")  # Log if data fetch fails
        return

    for incident in vecji_obseg_data['value']:
        # Check if the incident has already been posted based on its content
        if not is_duplicate_incident(incident, posted_vecji_obseg):
            logger.info(f"New vecjiObseg incident found: ID {incident}. Posting...")  # Log the new incident found
            await post_vecji_obseg_incidents(context.bot, incident)
            posted_vecji_obseg.append(incident)  # Add the new incident to the list
            write_posted_vecji_obseg(posted_vecji_obseg_file, posted_vecji_obseg)
        else:
            logger.info(f"Incident ID {incident} is already posted. Skipping.")  # Log if the incident is already posted
            
# END Večji obseg

# Function to check if any keyword is present in the given text
def keyword_match(text, keywords):
    return any(keyword.lower() in text.lower() for keyword in keywords)

# Function to check and match dogodekNaziv keywords
def match_keywords_in_dogodek(dogodekNaziv):
    matched_topics = []
    for topic, keywords in keywords_map.items():
        if keyword_match(dogodekNaziv, keywords):
            matched_topics.append(topic)
    return matched_topics

# Function to handle retries for sending photos
async def retry_send_photo(bot, chat_id, photo, caption, message_thread_id=None, retries=5):
    """Send a photo with an HTML caption. Returns the sent Message, or None on failure."""
    healed = False
    for attempt in range(retries):
        try:
            with open(photo, 'rb') as img_file:
                return await bot.send_photo(chat_id=chat_id, photo=InputFile(img_file), caption=caption,
                                            parse_mode='HTML', message_thread_id=message_thread_id)
        except BadRequest as e:
            logger.error(f"Failed to send photo: {e}")
            if 'message thread not found' in str(e).lower():
                if not healed:
                    healed = True
                    new_id = await ensure_topic_thread(bot, chat_id, message_thread_id)
                    if new_id:
                        message_thread_id = new_id
                        continue
                logger.error(f"Invalid message thread ID: {message_thread_id}. Skipping this post.")
                break
            if 'caption is too long' in str(e).lower() or 'wrong file identifier' in str(e).lower():
                break
            await asyncio.sleep(5)
        except TelegramError as e:
            logger.error(f"Photo send attempt {attempt + 1}/{retries} failed: {e}")
            await asyncio.sleep(5)
    return None

def create_static_map_image(lat, lon, filename='incident_map.png', zoom=14, map_style='topo', saturation_level=0.7):
    """
    Create a static map image with a marker at the specified latitude and longitude.

    Parameters:
        lat (float): Latitude of the marker.
        lon (float): Longitude of the marker.
        filename (str): The output filename for the static map image.
        zoom (int): The zoom level for the static map image.
        map_style (str): The style of the map. Options: 'default', 'topo', 'light', 'dark'.
        saturation_level (float): The level of saturation to apply (0.0 for grayscale, 1.0 for original).

    Returns:
        None
    """
    # Map style URL templates
    style_urls = {
        # 'default': 'http://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'topo': 'http://a.tile.opentopomap.org/{z}/{x}/{y}.png',
        # 'light': 'http://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
        # 'dark': 'http://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'
    }

    # Get the URL template based on the selected map style
    url_template = style_urls.get(map_style, style_urls['topo'])

    # Create the static map with the chosen style
    m = StaticMap(800, 600, url_template=url_template)
    marker = CircleMarker((lon, lat), 'red', 12)
    m.add_marker(marker)

    # Render the map with the marker at the specified zoom level and save to a BytesIO object
    image_buffer = BytesIO()
    image = m.render(zoom=zoom)  # Render the image using StaticMap
    image.save(image_buffer, format='PNG')
    image_buffer.seek(0)  # Move cursor to the beginning of the BytesIO object

    # Open the image using PIL from the BytesIO object
    pil_image = Image.open(image_buffer)

    # Apply the saturation filter using ImageEnhance
    enhancer = ImageEnhance.Color(pil_image)
    image_enhanced = enhancer.enhance(saturation_level)

    # Save the enhanced image
    image_enhanced.save(filename)
    print(f"Map saved as {filename} with reduced saturation.")


# Fetch and parse RSS feed
def get_rss_feed(url):
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.content
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch RSS feed: {e}")
        return None

def parse_rss_feed(rss_content):
    if not rss_content:
        return []

    root = ET.fromstring(rss_content)
    incidents = []
    unique_guids = set()

    for item in root.findall(".//item"):
        guid = item.find("guid").text
        if guid not in unique_guids:
            unique_guids.add(guid)
            incidents.append({
                'id': item.find("link").text.split('/')[-1],
                'title': item.find("title").text,
                'link_suffix': item.find("link").text.split('/')[-1],
                'description': item.find("description").text,
                'pub_date': item.find("pubDate").text
            })
    return incidents

# Function to get incident details
def get_incident_details(link_suffix):
    incident_url = f"{incident_details_url_base}{link_suffix}"
    try:
        response = requests.get(incident_url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch incident details for {link_suffix}: {e}")
        return None

# Function to post incident data to the Telegram group topic
TELEGRAM_CAPTION_LIMIT = 1000


def build_incident_message(details, incident, emoji, nastanek, pub_date, lat, lon, besedilo_limit=None):
    """Compose the incident card. besedilo_limit trims the free-text field so a caption fits."""
    besedilo = details.get('besedilo', 'N/A')
    if besedilo_limit is not None and len(besedilo) > besedilo_limit:
        besedilo = besedilo[:max(0, besedilo_limit - 1)].rstrip() + u'\u2026'
    return (
        f"<b>{details.get('intervencijaVrstaNaziv', 'N/A')}</b>\n\n"
        f"<b>{details.get('obcinaNaziv', 'N/A')}</b>\n"
        f"<i>\u010cas dogodka: {nastanek}</i> \n\n"
        f"{besedilo}\n"
        f"<i>Lat:</i> {lat}\n"
        f"<i>Lon:</i> {lon}\n"
        f"<b>{details.get('dogodekNaziv', 'N/A')}</b>\n"
        f"{emoji}\n"
        f"<i>\u010cas objave:</i> {pub_date}\n"
        f"ID: <a href='https://spin3.sos112.si/javno/zemljevid/{incident['id']}'>{incident['id']}</a>"
    )


def compose_incident(details, incident, extra_trim=0):
    """Return (full message, caption trimmed to Telegram's photo caption limit).

    extra_trim leaves room for an extra footer (used by the radius notifications).
    """
    # The SOS112 API now exposes approximate coordinates (wgsLatApprox/wgsLonApprox);
    # use the exact fields when present and fall back to the approximate ones.
    lat = details.get('wgsLat') or details.get('wgsLatApprox')
    lon = details.get('wgsLon') or details.get('wgsLonApprox')
    dogodekNaziv = details.get('dogodekNaziv', '')
    besedilo = details.get('besedilo', '')
    intervencijaVrstaNaziv = details.get('intervencijaVrstaNaziv', '')

    emoji = get_emojis_for_keywords(dogodekNaziv, besedilo, intervencijaVrstaNaziv)

    nastanekCas = details.get('nastanekCas', 'N/A')
    formatted_nastanekCas = format_timestamp(nastanekCas) if nastanekCas != 'N/A' else 'N/A'

    pub_date = incident['pub_date']
    formatted_pub_date = format_timestamp(pub_date) if pub_date != 'N/A' else 'N/A'
    formatted_pub_date = formatted_pub_date.replace('GMT', 'UTC')

    for eng_day, slovenian_day in custom_day_names.items():
        formatted_pub_date = formatted_pub_date.replace(eng_day, slovenian_day)

    message = build_incident_message(details, incident, emoji, formatted_nastanekCas, formatted_pub_date, lat, lon)

    caption = message
    limit = TELEGRAM_CAPTION_LIMIT - extra_trim
    if len(caption) > limit:
        excess = len(caption) - limit
        caption = build_incident_message(
            details, incident, emoji, formatted_nastanekCas, formatted_pub_date, lat, lon,
            besedilo_limit=max(0, len(besedilo) - excess - 5))
        logger.info(f"Incident {incident['id']}: caption shortened to {len(caption)} chars.")
    return message, caption


async def post_incident_to_topic(bot, incident, topic_id, is_verified=True):
    """Post one incident to a thread (main thread when topic_id is None).

    Returns the sent Message so callers can keep its id, or None if nothing went out.
    """
    detailed_data = get_incident_details(incident['link_suffix'])
    if not detailed_data or 'value' not in detailed_data:
        logger.warning(f"No details found for incident {incident['id']}. Skipping.")
        return None

    details = detailed_data['value']
    lat = details.get('wgsLat') or details.get('wgsLatApprox')
    lon = details.get('wgsLon') or details.get('wgsLonApprox')
    message, caption = compose_incident(details, incident)

    if lat and lon:
        try:
            create_static_map_image(lat, lon)
        except Exception as e:
            logger.error(f"Map generation failed for incident {incident['id']}: {type(e).__name__}: {e}")
        else:
            sent = await retry_send_photo(bot, TELEGRAM_GROUP_ID, 'incident_map.png', caption, message_thread_id=topic_id)
            if sent is not None:
                logger.info(f"Posted incident {incident['id']} with map image (thread {topic_id}, verified={is_verified}).")
                return sent
            logger.error(f"Photo post failed for incident {incident['id']}; falling back to text.")
    return await retry_send_message(bot, TELEGRAM_GROUP_ID, message, message_thread_id=topic_id)

async def notify_radius_subscribers(bot, incident, details, incident_topics=None):
    """Send a new incident to every private chat whose tracked radius covers it.

    ``incident_topics`` lists the kinds this incident belongs to - the same names
    the group topics use (intervention type plus keyword topics). A chat that
    followed only some kinds is skipped when none of them apply; when the set is
    empty the incident counts as unclassified and reaches every matching area.

    Failures for one subscriber never abort the others, and each (chat, incident)
    pair is recorded so a restart cannot deliver the same incident twice.
    """
    lat = details.get('wgsLat') or details.get('wgsLatApprox')
    lon = details.get('wgsLon') or details.get('wgsLonApprox')
    if not (lat and lon):
        return
    if incident_topics is None:
        incident_topics = set(match_keywords_in_dogodek(details.get('dogodekNaziv', '')))
        intervention = (details.get('intervencijaVrstaNaziv') or '').strip()
        if intervention:
            incident_topics.add(intervention)
    try:
        matches = subscriptions.match_subscribers(lat, lon, incident_topics)
    except Exception as e:
        logger.error(f"Subscription lookup failed for incident {incident['id']}: {type(e).__name__}: {e}")
        return
    if not matches:
        return

    photo_path = None
    try:
        create_static_map_image(float(lat), float(lon), filename='subscriber_map.png')
        photo_path = 'subscriber_map.png'
    except Exception as e:
        logger.warning(f"Subscriber map generation failed for incident {incident['id']}: {type(e).__name__}: {e}")

    async def _send_photo(_bot, chat_id, text):
        if not photo_path:
            raise TelegramError("no map image")
        with open(photo_path, 'rb') as photo_file:
            await _bot.send_photo(chat_id=chat_id, photo=photo_file,
                                  caption=text, parse_mode='HTML')
        return True

    async def _send_text(_bot, chat_id, text):
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML',
                                disable_web_page_preview=True)

    delivered = 0
    for chat_id, chat_matches in matches.items():
        try:
            if subscriptions.already_delivered(chat_id, incident['id']):
                continue
            note = subscriptions.format_match_note(chat_matches)
            _message, caption = compose_incident(details, incident, extra_trim=len(note) + 12)
            if await subscriptions.deliver(bot, chat_id, chat_matches, caption, _send_photo, _send_text):
                subscriptions.record_delivery(chat_id, incident['id'])
                delivered += 1
        except Exception as e:
            logger.error(f"Subscriber delivery to {chat_id} failed: {type(e).__name__}: {e}")
        await asyncio.sleep(0.5)

    if photo_path:
        try:
            os.remove(photo_path)
        except OSError:
            pass
    if delivered:
        logger.info(f"Incident {incident['id']} sent to {delivered} subscriber(s).")


# Function to fetch and post new incidents automatically every 3 minutes
async def auto_fetch_and_post(context: CallbackContext, initial_run=False):
    global fetched_incidents
    
    logger.info("Checking for new incidents in the RSS feed...")  # Log the start of the check

    if not fetched_incidents:
        fetched_incidents.update(read_posted_incidents(posted_incidents_file))

    # Fetch RSS content
    rss_content = get_rss_feed(rss_feed_url)
    incidents = parse_rss_feed(rss_content)
    
    if not incidents:
        logger.warning("No incidents found in the RSS feed.")  # Log if no incidents are found
        return
    
    # Reverse the order of incidents if it's the initial run
    if initial_run:
        incidents.reverse()  # Post oldest first for the initial run

    # Loop through and post each incident
    for incident in incidents:
        incident_id = incident['id']
        logger.info(f"Checking incident ID: {incident_id}")  # Log the incident being checked
        
        if incident['id'] not in fetched_incidents:
            logger.info(f"New incident found: ID {incident_id}. Posting...")  # Log the new incident found
            # Variable to track if the incident is posted in any topic
            posted_anywhere = False

            # First, post to the general "All" topic if not posted to any category-specific topics
            await post_incident_to_topic(context.bot, incident, topics["All"])

            # Get incident details and check for further categorization
            detailed_data = get_incident_details(incident['link_suffix'])
            if detailed_data and 'value' in detailed_data:
                details = detailed_data['value']
                location = details.get('obcinaNaziv', '').upper()
                intervention_type = details.get('intervencijaVrstaNaziv', '')
                dogodekNaziv = details.get('dogodekNaziv', '')

                lat = details.get('wgsLat') or details.get('wgsLatApprox')
                lon = details.get('wgsLon') or details.get('wgsLonApprox')
                
                # Determine the region from the coordinates
                if lat and lon:
                    region = get_region_from_coordinates(lat, lon)
                    if region in topics:
                        await post_incident_to_topic(context.bot, incident, topics[region])
                        posted_anywhere = True
                        logger.info(f"Posted incident ID {incident_id} to region topic: {region}")  # Log successful post
                        await asyncio.sleep(3)

                # Post to the topic based on the intervention type if it matches any known topics
                if intervention_type in topics:
                    await post_incident_to_topic(context.bot, incident, topics[intervention_type])
                    posted_anywhere = True
                    logger.info(f"Posted incident ID {incident_id} to intervention type topic: {intervention_type}")  # Log successful post
                    await asyncio.sleep(1)

                # Check for keyword matches in the dogodekNaziv and post to those topics
                matched_topics = match_keywords_in_dogodek(dogodekNaziv)
                for matched_topic in matched_topics:
                    if matched_topic in topics:
                        await post_incident_to_topic(context.bot, incident, topics[matched_topic])
                        posted_anywhere = True
                        logger.info(f"Posted incident ID {incident_id} to keyword topic: {matched_topic}")  # Log successful post
                        await asyncio.sleep(1)

                # Send the incident to every private chat that tracks this area,
                # limited to the incident kinds each chat chose to follow
                incident_topics = {intervention_type} | set(matched_topics)
                incident_topics.discard('')
                incident_topics.discard('N/A')
                await notify_radius_subscribers(context.bot, incident, details,
                                                incident_topics)

            # # If posted anywhere, delete from the All topic to avoid duplication
            # if posted_anywhere:
            #     # Remove from All topic if necessary (Add logic to delete post from All topic if needed)
            #     pass

            # After posting, add the ID to the fetched_incidents set and save it
            fetched_incidents.add(incident_id)
            
            # Immediately write the updated set to the JSON file after each new incident post
            write_posted_incidents(posted_incidents_file, fetched_incidents)
            logger.info(f"Incident ID {incident_id} written to {posted_incidents_file}.")
        else:
            logger.info(f"Incident ID {incident_id} is already posted. Skipping.")  # Log if the incident is already posted

# Function to read posted incidents from a JSON file
def read_posted_incidents(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return set(json.load(file))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

# Function to write posted incidents to a JSON file
def write_posted_incidents(file_path, incident_ids):
    # Convert the set of incident IDs to a list and sort it
    sorted_incident_ids = sorted(list(incident_ids))

    # Keep only the most recent MAX_STORED_REPORTS incidents
    if len(sorted_incident_ids) > MAX_STORED_REPORTS:
        sorted_incident_ids = sorted_incident_ids[-MAX_STORED_REPORTS:]

    # Write the limited list of IDs back to the JSON file
    with open(file_path, 'w', encoding='utf-8') as file:
        json.dump(sorted_incident_ids, file, ensure_ascii=False, indent=4)

def store_message_id(incident_id, message_id, chat_id, file_path):
    """Remember which message carried an incident, so it can be deleted later."""
    message_data = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                message_data = json.load(file)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read {file_path}: {e}. Starting a fresh map.")
    message_data[incident_id] = {'message_id': message_id, 'chat_id': chat_id}
    try:
        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(message_data, file, ensure_ascii=False, indent=4)
    except OSError as e:
        logger.error(f"Could not write {file_path}: {e}")


def read_posted_nepreverjeni_incidents(file_path):
    """IDs of unverified reports already announced."""
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return set(json.load(file))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def write_posted_nepreverjeni_incidents(file_path, incident_ids):
    sorted_incident_ids = sorted(list(incident_ids))
    if len(sorted_incident_ids) > MAX_STORED_REPORTS:
        sorted_incident_ids = sorted_incident_ids[-MAX_STORED_REPORTS:]
    with open(file_path, 'w', encoding='utf-8') as file:
        json.dump(sorted_incident_ids, file, ensure_ascii=False, indent=4)


async def auto_fetch_and_post_nepreverjeni(context: CallbackContext, initial_run=False):
    """Announce unverified first reports in their own topic.

    The full feed lists every active report; a report is still unverified while it
    has no intervention type or no description. Announced IDs are remembered so a
    report is never posted twice, and the very first run only seeds that list
    instead of backfilling the whole topic.
    """
    logger.info("Checking for new unverified incidents in the RSS feed...")
    state_path = posted_nepreverjeni_incidents_file
    first_time = not os.path.exists(state_path)
    handled = read_posted_nepreverjeni_incidents(state_path)

    rss_content = get_rss_feed("https://spin3.sos112.si/api/javno/ODRSS/true")
    incidents = parse_rss_feed(rss_content)
    logger.info(f"Number of unverified incidents found: {len(incidents)}")
    if not incidents:
        return

    if initial_run:
        incidents.reverse()  # oldest first

    if first_time:
        seeded = {incident['id'] for incident in incidents}
        write_posted_nepreverjeni_incidents(state_path, seeded)
        logger.info(f"First run: seeded {len(seeded)} existing first reports without posting them.")
        return

    topic_id = topics["Prva prijava-Nepreverjen dogodek"]
    announced = 0
    for incident in incidents:
        incident_id = incident['id']
        if incident_id in handled:
            continue

        detailed_data = get_incident_details(incident['link_suffix'])
        if not detailed_data or 'value' not in detailed_data:
            continue  # try again on the next pass

        details = detailed_data['value']
        besedilo = details.get('besedilo', 'N/A')
        intervencijaVrstaNaziv = details.get('intervencijaVrstaNaziv', 'N/A')
        unverified = ((not besedilo or besedilo == 'N/A') or
                      (not intervencijaVrstaNaziv or intervencijaVrstaNaziv == 'N/A'))
        if not unverified:
            logger.info(f"Incident ID {incident_id} is not unverified. Skipping.")
            handled.add(incident_id)  # verified reports are announced by the main flow
            write_posted_nepreverjeni_incidents(state_path, handled)
            continue

        sent = await post_incident_to_topic(context.bot, incident, topic_id, is_verified=False)
        if sent is not None:
            store_message_id(incident_id, sent.message_id, TELEGRAM_GROUP_ID, posted_nepreverjeni_messages_file)
            announced += 1
            await asyncio.sleep(1)
        else:
            logger.warning(f"Failed to post unverified incident ID {incident_id}.")

        handled.add(incident_id)
        write_posted_nepreverjeni_incidents(state_path, handled)

    logger.info(f"Unverified pass done: {announced} new first report(s) announced.")



async def error_handler(update: Update, context: CallbackContext):
    logger.error(f"An error occurred: {context.error}")
    
async def log_topic_capabilities(application):
    """Tell the operator whether a deleted topic can be recreated automatically."""
    expected = os.getenv('EXPECTED_BOT_USERNAME', 'nasoncnistranialpbot').lstrip('@')
    if application.bot.username and application.bot.username.lower() != expected.lower():
        logger.warning(
            f"Posting with @{application.bot.username}, but the production bot is "
            f"@{expected}. eusafetybot is retired; check TELEGRAM_BOT_TOKEN."
        )
    else:
        logger.info(f"Posting with the expected bot @{application.bot.username}")
    try:
        member = await application.bot.get_chat_member(
            chat_id=TELEGRAM_GROUP_ID, user_id=application.bot.id
        )
        can_manage = bool(getattr(member, 'can_manage_topics', False))
        logger.info(
            f"Bot @{application.bot.username} is '{member.status}' in {TELEGRAM_GROUP_ID}; "
            f"can_manage_topics={can_manage}"
        )
        if not can_manage:
            logger.warning(
                "Bot cannot manage topics: a deleted topic cannot be recreated. "
                "Grant 'Manage topics' in the group's admin settings for it."
            )
    except Exception as exc:  # informational only
        logger.warning(f"Could not read bot admin rights: {exc}")


async def register_bot_commands(application):
    """Expose the subscription commands in Telegram's command menu."""
    await log_topic_capabilities(application)
    try:
        await application.bot.set_my_commands([
            BotCommand("track", "Dodaj lokacijo (kraj, občina ali koordinate)"),
            BotCommand("list", "Prikaži sledene lokacije na zemljevidu"),
            BotCommand("remove", "Odstrani sledeno lokacijo"),
            BotCommand("stop", "Izbriši vse lokacije in prenehaj s sporočili"),
            BotCommand("topics", "Izberi vrste dogodkov za lokacijo"),
            BotCommand("help", "Podrobna navodila"),
        ])
    except Exception as exc:  # network hiccup must not stop the bot
        logger.warning(f"Could not register bot commands: {exc}")


# Main function to start the bot
def main():
    load_topic_overrides()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(register_bot_commands).build()
    job_queue = application.job_queue

    # Private-chat radius subscriptions (/track, /list, /remove, /stop)
    subscriptions.prune_deliveries()
    for handler in subscriptions.build_handlers():
        application.add_handler(handler)

    # Run fetching and posting for regular incidents and vecjiObseg incidents
    job_queue.run_repeating(auto_fetch_and_post, interval=80, first=0)
    job_queue.run_repeating(fetch_and_post_vecji_obseg, interval=80, first=60)  # Run vecjiObseg fetch every 150 seconds, offset by 60 seconds
    job_queue.run_repeating(auto_fetch_and_post_nepreverjeni, interval=120, first=110)  # first reports topic
    
    application.add_error_handler(error_handler)

    application.run_polling()
    logger.info("Bot started and will automatically fetch and post new incidents every 2 minutes...")

if __name__ == "__main__":
    main()
