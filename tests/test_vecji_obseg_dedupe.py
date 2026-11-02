"""Večji obseg dedupe: one alert, one post.

The feed rewrites a running alert on every refresh and only the clock window in
the body moves ("smo do 17:30 …" → "do 17:35" → "do 17:40"). Comparing whole
objects made that look like a new incident every poll, so the same alert landed
in the "Večji obseg" topic every 5 minutes. Alerts are now compared on their
core: municipality + event time + the text with the moving window masked out.

Runs offline.

    TELEGRAM_BOT_TOKEN=1:T TELEGRAM_GROUP_ID=-1001 python tests/test_vecji_obseg_dedupe.py
"""

import asyncio
import json
import os
import sys

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:TEST")
os.environ.setdefault("TELEGRAM_GROUP_ID", "-1001234567890")
os.environ["SUBSCRIPTIONS_DB"] = os.path.join(BOT_DIR, "tests", "_vecji_tmp.db")
sys.path.insert(0, BOT_DIR)

import SPIN112 as B  # noqa: E402

PASSES = []
FAILS = []
STATE = os.path.join(BOT_DIR, "tests", "_vecji_posted_tmp.json")
DATUM = "2026-11-02T13:50:00.24"


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print("PASS", name)
    else:
        FAILS.append(name)
        print("FAIL", name, detail)


def alert(window="17:25", prijave=1, druzba=1, obcina="Vipava",
          datum=DATUM, kind="Močan veter", tail="Enote o poškodovanih in številu poškodb še ne poročajo."):
    besedilo = (
        f"V času dogodkov večjega obsega {kind} smo do {window} na območju {obcina} "
        f"zabeležili več prijav dogodkov ({prijave}). Aktivirali smo Prostovoljna "
        f"teritorialna gasilska društva ({druzba}). {tail}"
    )
    return {
        "obcinaMID": 136,
        "obcinaNaziv": obcina,
        "besediloList": [{"datum": datum, "besedilo": besedilo}],
    }


# --- 1. the reported repetition: same alert, window advances ------------------
seen = [alert("17:25")]
check("nothing posted yet -> not a duplicate", B.is_duplicate_incident(alert("17:25"), []) is False)
check("identical alert -> duplicate", B.is_duplicate_incident(alert("17:25"), seen) is True)
check("17:25 then 17:30 -> duplicate", B.is_duplicate_incident(alert("17:30"), seen) is True)
check("17:25 then 17:35 -> duplicate", B.is_duplicate_incident(alert("17:35"), seen) is True)
check("17:25 then 19:05 -> duplicate", B.is_duplicate_incident(alert("19:05"), seen) is True)
check("dot separator 17.40 -> duplicate", B.is_duplicate_incident(alert("17.40"), seen) is True)
check("single-digit hour 9:05 -> duplicate", B.is_duplicate_incident(alert("9:05"), seen) is True)

# --- 2. real changes must still be posted ------------------------------------
check("second report count -> not duplicate",
      B.is_duplicate_incident(alert("17:35", prijave=2), seen) is False)
check("second activated unit -> not duplicate",
      B.is_duplicate_incident(alert("17:35", druzba=2), seen) is False)
check("casualties reported -> not duplicate",
      B.is_duplicate_incident(alert("17:35", tail="Enote poročajo o 1 poškodovani osebi."), seen) is False)
check("other municipality -> not duplicate",
      B.is_duplicate_incident(alert("17:35", obcina="Ajdovščina"), seen) is False)
check("other event time -> not duplicate",
      B.is_duplicate_incident(alert("17:35", datum="2026-11-02T15:10:00.11"), seen) is False)
check("other hazard kind -> not duplicate",
      B.is_duplicate_incident(alert("17:35", kind="Poplavljanje"), seen) is False)
check("an unrelated already-complained clock is kept",
      B.vecji_obseg_core({"obcinaNaziv": "Kozina", "besediloList": [
          {"datum": DATUM, "besedilo": "Ob 9.08 je na Bazoviški cesti močan veter poškodoval nadstrešek."}]}
      ) != B.vecji_obseg_core({"obcinaNaziv": "Kozina", "besediloList": [
          {"datum": DATUM, "besedilo": "Ob 10.15 je na Bazoviški cesti močan veter poškodoval nadstrešek."}]}))

# --- 3. core fingerprint is stable and ignores formatting noise --------------
core_a = B.vecji_obseg_core(alert("17:30"))
core_b = B.vecji_obseg_core(alert("17:35"))
check("core equal across window change", core_a == core_b)
check("core masks the window", "do <cas>" in core_a and "17:30" not in core_a)
check("core keeps municipality", "vipava" in core_a)
check("core keeps event time", DATUM in core_a)
spaced = alert("17:30")
spaced["besediloList"][0]["besedilo"] = "  " + spaced["besediloList"][0]["besedilo"].replace(" ", "   ") + "\n"
check("core tolerates whitespace noise", B.vecji_obseg_core(spaced) == core_a)
check("empty incident does not raise", B.vecji_obseg_core({}) == "")

# --- 4. legacy state file (whole objects, no core) still matches -------------
legacy = [alert("17:20"), alert("17:50")]
check("legacy entry still recognised", B.is_duplicate_incident(alert("18:05"), legacy) is True)

# --- 5. the poll loop posts once, not on every tick --------------------------
CALLS = []


async def fake_post(bot, incident):
    CALLS.append(incident["besediloList"][0]["besedilo"])


class FakeContext:
    bot = object()


def run_ticks(feed):
    B.posted_vecji_obseg_file = STATE
    B.post_vecji_obseg_incidents = fake_post
    B.get_vecji_obseg_data = lambda url: {"value": feed}
    asyncio.run(B.fetch_and_post_vecji_obseg(FakeContext()))


if os.path.exists(STATE):
    os.remove(STATE)
for window in ("17:25", "17:30", "17:35", "17:40"):
    run_ticks([alert(window)])
check("four identical ticks -> one post", len(CALLS) == 1, f"posts={len(CALLS)}")

run_ticks([alert("17:45", prijave=2)])
check("real change after repetition -> second post", len(CALLS) == 2, f"posts={len(CALLS)}")
run_ticks([alert("17:50", prijave=2)])
check("window-only tick after a change -> no post", len(CALLS) == 2, f"posts={len(CALLS)}")

with open(STATE, "r", encoding="utf-8") as fh:
    stored = json.load(fh)
check("state file holds the two posted alerts", len(stored) == 2, f"stored={len(stored)}")
check("state file kept the newest alert", "17:45" in stored[-1]["besediloList"][0]["besedilo"])

# a second, unrelated alert in the same feed is still posted
run_ticks([alert("17:50", prijave=2), alert("18:02", obcina="Ajdovščina")])
check("new municipality in the same feed -> new post", len(CALLS) == 3, f"posts={len(CALLS)}")
run_ticks([alert("17:55", prijave=2), alert("18:07", obcina="Ajdovščina")])
check("both alerts only moving their windows -> no post", len(CALLS) == 3, f"posts={len(CALLS)}")

if os.path.exists(STATE):
    os.remove(STATE)

print()
print(f"{len(PASSES)} passed, {len(FAILS)} failed")
if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
