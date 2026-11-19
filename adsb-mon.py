"""
adsb-mon — RTL-SDR / R820T
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Requirements:
  pip install pyrtlsdr==0.2.91 numpy tkintermapview pillow
  
Hardware:
  RTL-SDR / R820T dongle with WinUSB driver (set up via Zadig).
  Requires librtlsdr.dll, libusb-1.0.dll, libwinpthread-1.dll
  in the same folder as this script (or on PATH). Ex.: for
  Windows: set PATH=%CD%;%PATH%
  Falls back to a 5-aircraft demo mode if no dongle is detected.

  macOS: brew install rtl-sdr (Homebrew lib dir is added automatically).

Python: 3.10+   Platform: Windows 11, macOS

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
External APIs (all free, no key required):
  hexdb.io            → aircraft registration, type, manufacturer,
                        operator, and flight route by ICAO hex
  planespotters.net   → aircraft photos by ICAO hex or registration
  tar1090-db (GitHub) → offline registration, type and military flag,
                        downloaded to ~/.cache/adsb-mon (refreshed monthly)
  OpenStreetMap       → map tiles via tkintermapview

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Features:
  • Tunes dongle to 1090 MHz and decodes Mode-S / ADS-B messages
  • Live table: ICAO, callsign, altitude, lat/lon, speed, heading,
    vertical rate, squawk, message count, last-seen (local time)
  • Detail panel: click any row to see full aircraft info + photo
  • Live map (OpenStreetMap): white triangle icons rotate with
    heading, cyan trail shows flight path (last ~3 min)
  • Hover over a map marker → tooltip with altitude, speed,
    operator, registration, route, squawk
  • Click a map marker → selects aircraft in table + detail panel
  • Online enrichment runs in background per aircraft (hexdb.io)
  • Photos fetched from planespotters.net after enrichment
  • ▶ Start / ⏹ Stop / 🗑 Clear controls
  • Log pane shows status messages and selected-aircraft state
"""

import os
import sys
import types
import tkinter as tk
from tkinter import ttk, scrolledtext
import threading
import time
import math
import queue
import io
import gzip
import re
import urllib.request
import urllib.error
import json
from collections import deque
from datetime import datetime
from pathlib import Path

# ── Optional heavy deps ──────────────────────────────────────────────────────
# pyrtlsdr imports pkg_resources only to read its own version (inside a bare
# try/except); setuptools no longer ships it, so a stub keeps the import alive.
try:
    import pkg_resources  # noqa: F401
except ImportError:
    sys.modules["pkg_resources"] = types.ModuleType("pkg_resources")

# ctypes.util.find_library doesn't search Homebrew's lib dir on macOS, and
# pyrtlsdr then fails with AttributeError instead of ImportError.
if sys.platform == "darwin":
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(
        p for p in ("/opt/homebrew/lib", "/usr/local/lib",
                    os.environ.get("DYLD_FALLBACK_LIBRARY_PATH")) if p)

HW_ERROR = ""
try:
    from rtlsdr import RtlSdr
    HW_AVAILABLE = True
except Exception as e:
    HW_AVAILABLE = False
    HW_ERROR = f"{type(e).__name__}: {e}"

try:
    import numpy as np
    NUMPY_OK = True
except ImportError:
    NUMPY_OK = False

try:
    import tkintermapview
    MAP_OK = True
except ImportError:
    MAP_OK = False

try:
    from PIL import Image, ImageTk
    PIL_OK = True
except ImportError:
    PIL_OK = False

# ═══════════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════════
ADSB_FREQ        = 1_090_000_000
SAMPLE_RATE      = 2_000_000
GAIN             = 49.6
SAMPLES_PER_POLL = 256 * 1024
CS_CHARSET       = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ#####_###############0123456789######"

# ICAO → IATA airline designators, for an instant flight number before the
# adsbdb.com lookup answers (or when it can't be reached).
ICAO_TO_IATA = {
    "ACA": "AC", "AAL": "AA", "AFR": "AF", "AMX": "AM", "ANA": "NH",
    "ASA": "AS", "AUA": "OS", "AZA": "AZ", "BAW": "BA", "CCA": "CA",
    "CES": "MU", "CPA": "CX", "CSN": "CZ", "DAL": "DL", "DLH": "LH",
    "EIN": "EI", "ETD": "EY", "EZY": "U2", "FDX": "FX", "FIN": "AY",
    "IBE": "IB", "JAL": "JL", "JBU": "B6", "JZA": "QK", "KAL": "KE",
    "KLM": "KL", "NKS": "NK", "QFA": "QF", "QTR": "QR", "RPA": "YX",
    "RYR": "FR", "SAS": "SK", "SIA": "SQ", "SKW": "OO", "SWA": "WN",
    "SWR": "LX", "TAP": "TP", "THY": "TK", "UAE": "EK", "UAL": "UA",
    "UPS": "5X", "VIR": "VS", "VOI": "Y4", "WJA": "WS",
}


def is_partial(ac: dict) -> bool:
    """No callsign or position yet: only some of the aircraft's messages decoded so far."""
    return not ac["callsign"] or ac["lat"] is None


def flight_number(callsign: str) -> str:
    """ACA871 → AC871; '' for callsigns that aren't airline flights (C-GABC, N123AB)."""
    cs = (callsign or "").strip()
    iata = ICAO_TO_IATA.get(cs[:3])
    num = cs[3:].lstrip("0")
    return f"{iata}{num}" if iata and num and num[0].isdigit() else ""

BG      = "#0a0e1a"
PANEL   = "#111827"
PANEL2  = "#0f172a"
ACCENT  = "#00d4ff"
ACCENT2 = "#ff6b35"
GREEN   = "#4ade80"
FG      = "#e2e8f0"
MUTED   = "#b0bec5"
ROW_A   = "#111827"
ROW_B   = "#0f1623"
SEL     = "#1e3a5f"
FONT_MONO = ("Consolas", 10)
FONT_HEAD = ("Consolas", 10, "bold")

COLS = [
    ("ICAO",     7,  "center"),
    ("Callsign", 10, "center"),
    ("Flight",    8, "center"),
    ("Class",    10, "center"),
    ("Alt (ft)", 10, "e"),
    ("Lat",      11, "e"),
    ("Lon",      11, "e"),
    ("Spd (kt)", 10, "e"),
    ("Hdg (°)",  9,  "e"),
    ("V/R",      9,  "e"),
    ("Squawk",   8,  "center"),
    ("Msgs",     6,  "e"),
    ("Last Seen",12, "center"),
]

# ═══════════════════════════════════════════════════════════════════════════
#  CRC helpers
# ═══════════════════════════════════════════════════════════════════════════

def _crc24(data: bytes) -> int:
    GENERATOR = 0xFFF409
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= GENERATOR
    return crc & 0xFFFFFF

def _parity_rem(msg: bytes) -> int:
    """CRC XOR the parity field: 0 for a clean ADS-B frame, the address for DF0/4/5/16/20/21,
    the interrogator ID for DF11."""
    return _crc24(msg[:-3]) ^ int.from_bytes(msg[-3:], "big")

def _ap_address(msg: bytes) -> str:
    return f"{_parity_rem(msg):06X}"

# ═══════════════════════════════════════════════════════════════════════════
#  ADS-B / Mode-S decoder
# ═══════════════════════════════════════════════════════════════════════════

class ADSBDecoder:
    def __init__(self):
        self.aircraft: dict = {}
        self._cpr_cache: dict = {}
        self.db = AircraftDB()

    def demodulate(self, samples_iq) -> list:
        if not NUMPY_OK:
            return []
        iq  = samples_iq.astype(np.float32) - 127.5
        mag = np.hypot(iq[0::2], iq[1::2])
        messages, n, idx = [], len(mag), 0
        while idx < n - 240:
            p = mag[idx:idx+16]
            # dump1090's preamble test: pulses at 0, 2, 7, 9 are peaks, and the
            # gaps around them are quiet relative to the pulse level.
            if (p[0]>p[1] and p[1]<p[2] and p[2]>p[3] and p[3]<p[0] and
                    p[4]<p[0] and p[5]<p[0] and p[6]<p[0] and
                    p[7]>p[8] and p[8]<p[9] and p[9]>p[6]):
                high = (p[0]+p[2]+p[7]+p[9]) / 6
                if (p[4] >= high or p[5] >= high or p[11] >= high or
                        p[12] >= high or p[13] >= high or p[14] >= high):
                    idx += 1
                    continue
                msg = self._decode_frame(mag, idx+16)
                if msg:
                    messages.append(msg)
                    idx += 16 + len(msg)*16
                    continue
            idx += 1
        return messages

    def _decode_frame(self, mag, start):
        n, bits, pos = len(mag), [], start
        for _ in range(8):
            if pos+1 >= n: return None
            bits.append(1 if mag[pos] > mag[pos+1] else 0)
            pos += 2
        df = bits[0]<<4|bits[1]<<3|bits[2]<<2|bits[3]<<1|bits[4]
        total_bits = 112 if df >= 16 else 56
        for _ in range(total_bits - 8):
            if pos+1 >= n: return None
            bits.append(1 if mag[pos] > mag[pos+1] else 0)
            pos += 2
        if len(bits) < total_bits: return None
        raw = bytes(
            sum(bits[i+b] << (7-b) for b in range(8))
            for i in range(0, total_bits, 8)
        )
        rem = _parity_rem(raw)
        if df in (17, 18):
            return raw if rem == 0 else None
        if df == 11:
            # All-call reply: parity is CRC XOR interrogator ID (< 80). Only a clean
            # one may introduce a new address; others must name a known aircraft.
            known = raw[1:4].hex().upper() in self.aircraft
            return raw if rem == 0 or (rem < 80 and known) else None
        if df in (0, 4, 5, 16, 20, 21):
            # Parity is CRC XOR address, so any noise "decodes"; trust one only
            # when it names an aircraft already heard.
            return raw if f"{rem:06X}" in self.aircraft else None
        return None

    def parse(self, raw: bytes):
        if len(raw) < 7: return None
        df   = (raw[0] >> 3) & 0x1F
        icao = raw[1:4].hex().upper() if len(raw) >= 14 else "??????"
        info = {"icao": icao, "df": df, "ts": datetime.now()}
        if df in (17, 18):
            if len(raw) < 14: return None
            icao = raw[1:4].hex().upper()
            info["icao"] = icao
            me = raw[4:11]
            tc = (me[0] >> 3) & 0x1F
            if 1 <= tc <= 4:
                info["callsign"] = self._decode_callsign(me)
                if me[0] & 7:  # emitter category, e.g. A7 rotorcraft, B6 UAV
                    info["category"] = f"{'DCBA'[tc - 1]}{me[0] & 7}"
            elif 9 <= tc <= 18 or 20 <= tc <= 22:
                info["altitude"] = self._decode_altitude(me)
                lat_c, lon_c, odd = self._decode_cpr(me)
                info["_cpr"] = (lat_c, lon_c, odd)
                pos = self._resolve_cpr(icao, lat_c, lon_c, odd)
                if pos: info["lat"], info["lon"] = pos
            elif tc == 19:
                spd, hdg, vr = self._decode_velocity(me)
                if spd is not None: info["speed"]   = spd
                if hdg is not None: info["heading"] = hdg
                if vr  is not None: info["vrate"]   = vr
        elif df == 11:
            info["icao"] = raw[1:4].hex().upper()
            return info  # only says the aircraft is there
        elif df in (0, 4, 16, 20):
            info["icao"] = _ap_address(raw)
            info["altitude"] = self._decode_ac13((raw[2] & 0x1F) << 8 | raw[3])
        elif df in (5, 21):
            info["icao"] = _ap_address(raw)
            info["squawk"] = self._decode_squawk(raw)
        return info if len(info) > 3 else None

    def _decode_callsign(self, me):
        cs = ""
        for i in range(8):
            bi = 1 + i*6//8
            bo = (8 + i*6) % 8
            if bi+1 >= len(me): break
            val = ((me[bi] << 8 | me[bi+1]) >> (10-bo)) & 0x3F
            cs += CS_CHARSET[val] if val < len(CS_CHARSET) else "?"
        return cs.strip()

    def _decode_altitude(self, me):
        ac = (me[1] & 0xFF) << 4 | (me[2] & 0xF0) >> 4
        if ac & 0x10:  # Q bit: 25 ft increments
            n = ((ac & 0xFE0) >> 1) | (ac & 0xF)
            return n*25 - 1000
        return None

    def _decode_cpr(self, me):
        odd = (me[2] >> 2) & 1
        lat = ((me[2] & 3) << 15) | (me[3] << 7) | (me[4] >> 1)
        lon = ((me[4] & 1) << 16) | (me[5] << 8) | me[6]
        return lat/131072.0, lon/131072.0, odd

    def _resolve_cpr(self, icao, lat_c, lon_c, odd):
        c = self._cpr_cache.setdefault(icao, {})
        c[odd] = (lat_c, lon_c, time.time())
        if 0 in c and 1 in c and abs(c[0][2] - c[1][2]) < 10:
            return self._cpr_global(c[0][0], c[0][1], c[1][0], c[1][1], odd)
        return None

    @staticmethod
    def _cpr_global(la0, lo0, la1, lo1, odd):
        NZ = 15
        dl0 = 360/(4*NZ); dl1 = 360/(4*NZ-1)
        j   = math.floor(59*la0 - 60*la1 + .5)
        r0  = dl0*(j%60 + la0); r1 = dl1*(j%59 + la1)
        if r0 >= 270: r0 -= 360
        if r1 >= 270: r1 -= 360
        def NL(lat):
            if abs(lat) >= 87: return 1
            return math.floor(2*math.pi / math.acos(
                1 - (1 - math.cos(math.pi/(2*NZ))) / math.cos(math.radians(lat))**2))
        if NL(r0) != NL(r1): return None
        nl = NL(r0)
        if odd:
            ni = max(nl-1, 1); dl = 360/ni
            m  = math.floor(lo0*(nl-1) - lo1*nl + .5)
            lon = dl*(m%ni + lo1); lat = r1
        else:
            ni = max(nl, 1); dl = 360/ni
            m  = math.floor(lo0*(nl-1) - lo1*nl + .5)
            lon = dl*(m%ni + lo0); lat = r0
        if lon > 180: lon -= 360
        return lat, lon

    def _decode_velocity(self, me):
        spd = hdg = vr = None
        if (me[0] & 7) in (1, 2):
            es = (me[1] >> 2) & 1; ev = ((me[1] & 3) << 8) | me[2]
            ns = (me[3] >> 7) & 1; nv = ((me[3] & 0x7F) << 3) | (me[4] >> 5)
            if ev and nv:
                e  = ev-1 if not es else -(ev-1)
                nv2 = nv-1 if not ns else -(nv-1)
                spd = round(math.hypot(e, nv2))
                hdg = round(math.degrees(math.atan2(e, nv2)) % 360, 1)
            rs = (me[4] >> 3) & 1; rv = ((me[4] & 7) << 6) | (me[5] >> 2)
            if rv: vr = (rv-1)*64; vr = -vr if rs else vr
        return spd, hdg, vr

    @staticmethod
    def _decode_ac13(ac13):
        """13-bit altitude code, bits C1 A1 C2 A2 C4 A4 M B1 Q B2 D2 B4 D4 → feet, or None."""
        if not ac13 or ac13 & 0x40:  # 0 = no altitude; M bit = metres, unused in practice
            return None
        if ac13 & 0x10:  # Q bit: 25 ft steps
            n = ((ac13 & 0x1F80) >> 2) | ((ac13 & 0x20) >> 1) | (ac13 & 0xF)
            return n * 25 - 1000
        # Gillham code from older transponders: Gray-coded 500 ft and 100 ft steps.
        gray = 0
        for b in (10, 12, 1, 3, 5, 7, 9, 11, 0, 2, 4):  # D2 D4 A1 A2 A4 B1 B2 B4 C1 C2 C4
            gray = gray << 1 | (ac13 >> (12 - b)) & 1
        def gray_to_int(g):
            for shift in (8, 4, 2, 1):
                g ^= g >> shift
            return g
        n500, n100 = gray_to_int(gray >> 3), gray_to_int(gray & 7)
        if n100 in (0, 5, 6):
            return None
        if n100 == 7:
            n100 = 5
        if n500 % 2:
            n100 = 6 - n100
        return n500 * 500 + n100 * 100 - 1300

    def _decode_squawk(self, raw):
        # 13-bit identity, bits in order C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4
        idf = (raw[2] & 0x1F) << 8 | raw[3]
        def digit(b4, b2, b1):
            return sum(((idf >> (12 - b)) & 1) << s for b, s in ((b4, 2), (b2, 1), (b1, 0)))
        return f"{digit(5, 3, 1)}{digit(11, 9, 7)}{digit(4, 2, 0)}{digit(12, 10, 8)}"

    def update(self, raw: bytes):
        info = self.parse(raw)
        if not info: return None
        icao = info["icao"]
        ac = self.aircraft.setdefault(icao, {
            "icao": icao, "callsign": "", "altitude": None,
            "lat": None, "lon": None, "speed": None,
            "heading": None, "vrate": None, "squawk": "", "category": "",
            "msgs": 0, "last_seen": None,
            "reg": "", "type": "", "manufacturer": "", "operator": "",
            "photo_url": "", "route": "", "flight": "", "class": "",
            "_enriched": False, "_enriching": False, "_flight_cs": "",
        })
        cs = info.get("callsign")
        if cs and cs != ac["callsign"]:
            ac["flight"] = flight_number(cs)
        for k, v in info.items():
            if not k.startswith("_") and v is not None:
                ac[k] = v
        ac["msgs"] += 1
        ac["last_seen"] = info["ts"]
        self.db.classify(ac)
        return icao

# ═══════════════════════════════════════════════════════════════════════════
#  Online enrichment — hexdb.io (free, no key)
# ═══════════════════════════════════════════════════════════════════════════

ENRICH_RETRY = 60  # seconds before retrying a failed aircraft lookup

def _http_json(url: str, timeout=5):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "adsb-decoder/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {} if e.code == 404 else None  # {} = looked, not found
    except Exception:
        return None

def _http_bytes(url: str, timeout=6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "adsb-decoder/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None

def enrich_aircraft(ac: dict, update_q: queue.Queue):
    icao = ac["icao"]
    # 1. Aircraft info from hexdb.io
    data = _http_json(f"https://hexdb.io/api/v1/aircraft/{icao.lower()}")
    if data is None:  # lookup failed rather than found nothing; retry later
        ac["_enrich_after"] = time.time() + ENRICH_RETRY
        ac["_enriching"] = False
        return
    if data and "ICAOTypeCode" in data:
        ac["reg"]          = data.get("Registration", "")
        ac["type"]         = data.get("Type", "")
        ac["manufacturer"] = data.get("Manufacturer", "")
        ac["operator"]     = data.get("RegisteredOwners", "")
    # 2. Photo from Planespotters.net (free, no key, returns real photos)
    #    Try by hex first, fall back to registration
    photo_url = None
    ps = _http_json(f"https://api.planespotters.net/pub/photos/hex/{icao.lower()}")
    if ps and ps.get("photos"):
        thumb = ps["photos"][0].get("thumbnail_large", {}) or ps["photos"][0].get("thumbnail", {})
        photo_url = thumb.get("src") or ps["photos"][0].get("link")
    if not photo_url:
        reg = ac.get("reg", "").strip()
        if reg:
            ps2 = _http_json(f"https://api.planespotters.net/pub/photos/reg/{reg}")
            if ps2 and ps2.get("photos"):
                thumb = ps2["photos"][0].get("thumbnail_large", {}) or ps2["photos"][0].get("thumbnail", {})
                photo_url = thumb.get("src") or ps2["photos"][0].get("link")
    ac["photo_url"]  = photo_url or ""
    ac["_enriched"]  = True
    ac["_enriching"] = False
    update_q.put(("enrich", icao))

def enrich_flight(ac: dict, cs: str, update_q: queue.Queue):
    rd = _http_json(f"https://hexdb.io/api/v1/route/icao/{cs}")
    route = rd.get("route", "") if rd else ""
    flight = flight_number(cs)
    fd = _http_json(f"https://api.adsbdb.com/v0/callsign/{cs}")
    fr = fd.get("response") if fd else None
    if isinstance(fr, dict) and "flightroute" in fr:
        fr = fr["flightroute"]
        flight = fr.get("callsign_iata") or flight
        if not route and fr.get("origin") and fr.get("destination"):
            route = f"{fr['origin']['icao_code']}-{fr['destination']['icao_code']}"
    if ac.get("callsign") == cs:  # callsign may have changed mid-lookup
        ac["flight"], ac["route"] = flight, route
    update_q.put(("enrich", ac["icao"]))

def start_enrichment(ac: dict, update_q: queue.Queue):
    """Call on every update. Aircraft lookups run once; flight lookups run once
    the callsign arrives, which is usually several messages after the first."""
    if (not ac["_enriched"] and not ac["_enriching"]
            and time.time() >= ac.get("_enrich_after", 0)):
        ac["_enriching"] = True
        threading.Thread(target=enrich_aircraft, args=(ac, update_q), daemon=True).start()
    cs = ac.get("callsign") or ""
    if cs and cs != ac["_flight_cs"]:
        ac["_flight_cs"] = cs
        threading.Thread(target=enrich_flight, args=(ac, cs, update_q), daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════
#  Offline aircraft database — tar1090-db (no key)
# ═══════════════════════════════════════════════════════════════════════════

DB_DIR     = Path.home() / ".cache" / "adsb-mon"
DB_MAX_AGE = 30 * 86400
DB_FILES   = {  # local name → URL
    "aircraft.csv.gz": "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz",
    # Served gzipped despite the .js name: {type code: [name, "H1T"-style description, wake]}
    "types.json.gz":   "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/icao_aircraft_types2.js",
    "ranges.json":     "https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/ranges.json",
}

AIRLINE_CALLSIGN = re.compile(r"[A-Z]{3}\d")
CATEGORY_CLASS   = {"A7": "Heli", "B1": "Glider", "B2": "Balloon", "B4": "Ultralight", "B6": "UAV"}

def aircraft_class(ac: dict) -> str:
    """Military, Heli, Glider, Balloon, Ultralight, UAV, Commercial, Private, or ''."""
    if ac.get("_mil"):
        return "Military"
    if ac.get("_desc", "")[:1] == "H":
        return "Heli"
    if ac["category"] in CATEGORY_CLASS:
        return CATEGORY_CLASS[ac["category"]]
    cs = ac["callsign"]
    if not cs:
        return ""
    # Airline-style callsign (ACA871, RPA4567); GA flies under its registration.
    if cs != ac["reg"].replace("-", "") and AIRLINE_CALLSIGN.match(cs):
        return "Commercial"
    return "Private"

class AircraftDB:
    def __init__(self):
        self.records: dict = {}   # ICAO → "reg;type code;flags;name;year;owner;"
        self.types: dict = {}
        self.mil_ranges: list = []
        self.ready = False
        self._started = False

    def start(self, update_q: queue.Queue):
        if not self._started:
            self._started = True
            threading.Thread(target=self._load, args=(update_q,), daemon=True).start()

    def _load(self, update_q):
        try:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            for name, url in DB_FILES.items():
                path = DB_DIR / name
                if path.exists() and time.time() - path.stat().st_mtime < DB_MAX_AGE:
                    continue
                update_q.put(("log", f"⬇  Downloading aircraft database ({name})…"))
                data = _http_bytes(url, timeout=120)
                if data:
                    part = path.with_name(name + ".part")
                    part.write_bytes(data)
                    part.replace(path)
            records = {}
            with gzip.open(DB_DIR / "aircraft.csv.gz", "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    icao, _, rest = line.partition(";")
                    records[icao] = rest
            self.types = json.loads(gzip.decompress((DB_DIR / "types.json.gz").read_bytes()))
            self.mil_ranges = [(int(a, 16), int(b, 16)) for a, b in
                               json.loads((DB_DIR / "ranges.json").read_text())["military"]]
            self.records = records
            self.ready = True
            update_q.put(("log", f"📚  Aircraft database: {len(records):,} aircraft"))
        except Exception as e:
            update_q.put(("log", f"⚠  Aircraft database unavailable: {e}"))

    def classify(self, ac: dict):
        """Fill reg/type/operator gaps from the database and set ac["class"]."""
        if self.ready and "_db" not in ac:
            ac["_db"] = True
            reg, code, flags, name, _year, owner = (self.records.get(ac["icao"], "").split(";") + [""] * 6)[:6]
            n = int(ac["icao"], 16)
            # Flags are a bit string: military, interesting, PIA, LADD.
            ac["_mil"]  = flags[:1] == "1" or any(a <= n <= b for a, b in self.mil_ranges)
            ac["_desc"] = self.types.get(code, ["", ""])[1]
            ac["reg"]      = ac["reg"] or reg
            ac["type"]     = ac["type"] or name or self.types.get(code, [""])[0]
            ac["operator"] = ac["operator"] or owner
        ac["class"] = aircraft_class(ac)

# ═══════════════════════════════════════════════════════════════════════════
#  Demo source
# ═══════════════════════════════════════════════════════════════════════════

class DemoSource:
    FLEET = [
        ("C04FDE", "ACA871", 35000, 45.51, -73.56, 480, 275,   0, "7700"),
        ("A9C3B2", "UAL123", 28000, 45.60, -73.40, 520,  90, -64, "2100"),
        ("4CA7D1", "BAW456", 41000, 45.45, -73.70, 550, 180,   0, "0021"),
        ("3C6444", "AFR007", 33000, 45.55, -73.25, 490, 320, 128, "4521"),
        ("A0F16B", "DAL890", 29500, 45.38, -73.80, 430,  45, -32, "1200"),
    ]
    def __init__(self, decoder):
        self.decoder = decoder
        self._t = 0

    def tick(self):
        self._t += 1
        updates = []
        for icao, cs, alt, lat, lon, spd, hdg, vr, sq in self.FLEET:
            rad = math.radians(hdg)
            ac = self.decoder.aircraft.setdefault(icao, {
                "icao": icao, "callsign": cs, "altitude": alt,
                "lat": lat, "lon": lon, "speed": spd, "heading": hdg,
                "vrate": vr, "squawk": sq, "category": "", "msgs": 0, "last_seen": None,
                "reg": "", "type": "", "manufacturer": "", "operator": "",
                "photo_url": "", "route": "", "flight": flight_number(cs), "class": "",
                "_enriched": False, "_enriching": False, "_flight_cs": "",
            })
            ac["lat"] = lat + math.cos(rad)*spd/3600/60*self._t*0.1
            ac["lon"] = lon + math.sin(rad)*spd/3600/60/math.cos(math.radians(lat))*self._t*0.1
            ac["altitude"] = alt + (self._t % 5)*25*(1 if vr > 0 else -1)
            ac["msgs"] += 1
            ac["last_seen"] = datetime.now()
            self.decoder.db.classify(ac)
            updates.append(icao)
        return updates

# ═══════════════════════════════════════════════════════════════════════════
#  Receiver thread
# ═══════════════════════════════════════════════════════════════════════════

class ReceiverThread(threading.Thread):
    def __init__(self, decoder, update_q):
        super().__init__(daemon=True)
        self.decoder = decoder; self.q = update_q
        self.running = False; self.sdr = None
        self.demo = DemoSource(decoder)
        self.use_demo = not HW_AVAILABLE or not NUMPY_OK

    def run(self):
        self.running = True
        self.decoder.db.start(self.q)
        if self.use_demo: self._run_demo()
        else:             self._run_hw()

    def _run_demo(self):
        self.q.put(("status", "⚠  Demo mode — dongle/numpy not detected"))
        if HW_ERROR:
            self.q.put(("status", f"⚠  pyrtlsdr unavailable: {HW_ERROR}"))
        while self.running:
            for icao in self.demo.tick():
                self.q.put(("update", icao))
            time.sleep(1.5)

    def _run_hw(self):
        try:
            self.q.put(("status", "🔍  Scanning for RTL-SDR…"))
            try:
                from rtlsdr import librtlsdr
                cnt = librtlsdr.rtlsdr_get_device_count()
                if cnt == 0:
                    raise RuntimeError("No device found (count=0). Close SDR++ and replug.")
                names = [librtlsdr.rtlsdr_get_device_name(i).decode() for i in range(cnt)]
                self.q.put(("status", f"🔎  Found {cnt} device(s): {', '.join(names)}"))
            except ImportError:
                pass
            self.sdr = RtlSdr()
            self.sdr.sample_rate = SAMPLE_RATE
            self.sdr.center_freq = ADSB_FREQ
            self.sdr.gain        = GAIN
            self.q.put(("status", f"📡  Receiving on {ADSB_FREQ/1e6:.0f} MHz  |  gain {GAIN} dB"))
            while self.running:
                samples = self.sdr.read_bytes(SAMPLES_PER_POLL)
                arr     = np.frombuffer(samples, dtype=np.uint8)
                for msg in self.decoder.demodulate(arr):
                    icao = self.decoder.update(msg)
                    if icao: self.q.put(("update", icao))
        except Exception as e:
            self.q.put(("status", f"❌  SDR error: {e}"))
            self.q.put(("status", "⚠  Falling back to demo mode"))
            self.use_demo = True; self._run_demo()
        finally:
            if self.sdr:
                try: self.sdr.close()
                except Exception: pass

    def stop(self): self.running = False

# ═══════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("adsb-mon  ✈  1090 MHz")
        self.configure(bg=BG)
        self.geometry("1440x860")
        self.minsize(1000, 620)

        self.decoder        = ADSBDecoder()
        self.update_q       = queue.Queue()
        self.receiver       = None
        self._msg_count     = 0
        self._ac_seen: set  = set()
        self._selected_icao = None
        self._photo_img          = None
        self._photo_icao         = None   # icao whose photo is currently displayed
        self._photo_loading_icao = None   # icao currently being fetched
        self._map_markers: dict  = {}   # icao → CanvasMarkerData dict
        self._ac_trails:  dict   = {}   # icao → deque of (lat,lon)
        self._trail_paths: dict  = {}   # icao → path object
        self._canvas_items: dict = {}   # icao → {"cx":x, "cy":y}
        self._tooltip_win        = None
        self._tooltip_icao       = None
        self._last_zoom          = None

        self._build_ui()
        self._start_receiver()
        self._poll_queue()
        self.after(4000, self._map_refresh_loop)

    # ── UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._style()

        # Top bar
        top = tk.Frame(self, bg=BG, pady=6)
        top.pack(fill="x", padx=12)
        tk.Label(top, text="✈  ADS-B DECODER", font=("Consolas",16,"bold"),
                 fg=ACCENT, bg=BG).pack(side="left")
        self.lbl_status = tk.Label(top, text="Initialising…", font=FONT_MONO, fg=MUTED, bg=BG)
        self.lbl_status.pack(side="left", padx=20)
        right_bar = tk.Frame(top, bg=BG); right_bar.pack(side="right")
        self.lbl_ac   = tk.Label(right_bar, text="Aircraft: 0", font=FONT_HEAD, fg=ACCENT2, bg=BG)
        self.lbl_ac.pack(side="left", padx=10)
        self.lbl_msgs = tk.Label(right_bar, text="Messages: 0", font=FONT_HEAD, fg=ACCENT, bg=BG)
        self.lbl_msgs.pack(side="left", padx=10)
        for txt, cmd, fg in [("▶ Start",self._start,GREEN),("⏹ Stop",self._stop,ACCENT2),("🗑 Clear",self._clear,FG)]:
            tk.Button(right_bar, text=txt, command=cmd, bg="#1e293b", fg=fg,
                      font=FONT_HEAD, relief="flat", padx=8, pady=2,
                      cursor="hand2").pack(side="left", padx=4)

        tk.Frame(self, bg=ACCENT, height=1).pack(fill="x", padx=12)

        # Horizontal pane: left table+log | right detail+map
        pane = tk.PanedWindow(self, orient="horizontal", bg=BG,
                              sashwidth=5, sashrelief="flat", bd=0)
        pane.pack(fill="both", expand=True, padx=6, pady=6)

        # LEFT
        left = tk.Frame(pane, bg=BG)
        pane.add(left, minsize=640)

        tbl_f = tk.Frame(left, bg=BG)
        tbl_f.pack(fill="both", expand=True)
        vsb = ttk.Scrollbar(tbl_f, orient="vertical");   vsb.pack(side="right", fill="y")
        hsb = ttk.Scrollbar(tbl_f, orient="horizontal"); hsb.pack(side="bottom", fill="x")
        self.tree = ttk.Treeview(tbl_f, columns=[c[0] for c in COLS],
                                  show="headings", style="ADSB.Treeview",
                                  yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.config(command=self.tree.yview)
        hsb.config(command=self.tree.xview)
        self.tree.pack(fill="both", expand=True)
        for name, w, anchor in COLS:
            self.tree.heading(name, text=name)
            self.tree.column(name, width=w*9, anchor=anchor, minwidth=55)
        self.tree.tag_configure("even", background=ROW_A, foreground=FG)
        self.tree.tag_configure("odd",  background=ROW_B, foreground=FG)
        self.tree.tag_configure("new",  background="#0d2a1a", foreground=GREEN)
        self.tree.tag_configure("partial", font=("Consolas", 10, "italic"))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        tk.Frame(left, bg=ACCENT, height=1).pack(fill="x", pady=(4,0))
        log_f = tk.Frame(left, bg=BG, height=180); log_f.pack(fill="x")
        log_f.pack_propagate(False)
        tk.Label(log_f, text="LOG", font=("Consolas",8,"bold"), fg=MUTED, bg=BG).pack(anchor="w", padx=4)
        self.log = scrolledtext.ScrolledText(log_f, bg="#080c14", fg="#94a3b8",
                                              font=("Consolas",8), relief="flat",
                                              state="disabled", insertbackground=ACCENT)
        self.log.pack(fill="both", expand=True, padx=4, pady=2)

        # RIGHT
        right = tk.Frame(pane, bg=BG)
        pane.add(right, minsize=390)

        # Detail card
        self.card = tk.Frame(right, bg=PANEL)
        self.card.pack(fill="x", padx=4, pady=(0,4))
        self._build_detail_card()

        # Map
        tk.Label(right, text="LIVE MAP", font=("Consolas",8,"bold"),
                 fg=MUTED, bg=BG).pack(anchor="w", padx=8)
        if MAP_OK:
            self.map_widget = tkintermapview.TkinterMapView(right, corner_radius=0)
            self.map_widget.pack(fill="both", expand=True, padx=4, pady=(0,4))
            self.map_widget.set_position(45.5, -73.6)
            self.map_widget.set_zoom(7)
            self.after(500, self._setup_map_hover)   # defer until canvas is ready
        else:
            tk.Label(right,
                     text="Map unavailable\npip install tkintermapview",
                     fg=MUTED, bg=PANEL, font=FONT_MONO,
                     justify="center").pack(expand=True, fill="both")
            self.map_widget = None

    def _build_detail_card(self):
        for w in self.card.winfo_children(): w.destroy()

        hdr = tk.Frame(self.card, bg=PANEL); hdr.pack(fill="x", padx=8, pady=(6,2))
        tk.Label(hdr, text="AIRCRAFT DETAILS", font=("Consolas",9,"bold"),
                 fg=ACCENT, bg=PANEL).pack(side="left")
        self.lbl_enrich = tk.Label(hdr, text="", font=("Consolas",8),
                                    fg=GREEN, bg=PANEL)
        self.lbl_enrich.pack(side="right")
        tk.Frame(self.card, bg=ACCENT, height=1).pack(fill="x", padx=8)

        body = tk.Frame(self.card, bg=PANEL); body.pack(fill="x", padx=8, pady=6)

        # Photo
        photo_col = tk.Frame(body, bg="#050912", width=180, height=110)
        photo_col.pack(side="left", padx=(0,10))
        photo_col.pack_propagate(False)
        self.lbl_photo = tk.Label(photo_col, bg="#050912",
                                   text="No photo\navailable",
                                   fg=MUTED, font=("Consolas",8),
                                   justify="center")
        self.lbl_photo.pack(fill="both", expand=True)

        # Info fields
        info_col = tk.Frame(body, bg=PANEL); info_col.pack(side="left", fill="x", expand=True)
        self._dvars: dict = {}
        fields = [
            ("ICAO",         "icao"),
            ("Callsign",     "callsign"),
            ("Flight",       "flight"),
            ("Registration", "reg"),
            ("Type",         "type"),
            ("Class",        "class"),
            ("Manufacturer", "manufacturer"),
            ("Operator",     "operator"),
            ("Route",        "route"),
            ("Altitude",     "alt"),
            ("Speed",        "spd"),
            ("Heading",      "hdg"),
            ("Squawk",       "squawk"),
        ]
        for label, key in fields:
            row = tk.Frame(info_col, bg=PANEL); row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{label}:", font=("Consolas",8,"bold"),
                     fg=MUTED, bg=PANEL, width=13, anchor="w").pack(side="left")
            v = tk.StringVar(value="—"); self._dvars[key] = v
            tk.Label(row, textvariable=v, font=("Consolas",9),
                     fg=FG, bg=PANEL, anchor="w").pack(side="left")

        self.lbl_hint = tk.Label(self.card,
            text="← Click a row to see details & photo",
            font=("Consolas",9,"italic"), fg=MUTED, bg=PANEL)
        self.lbl_hint.pack(pady=(0,6))

    def _style(self):
        s = ttk.Style(self); s.theme_use("clam")
        s.configure("ADSB.Treeview", background=ROW_A, foreground=FG,
                     fieldbackground=ROW_A, rowheight=24, font=FONT_MONO, borderwidth=0)
        s.configure("ADSB.Treeview.Heading", background=PANEL, foreground=ACCENT,
                     font=FONT_HEAD, relief="flat")
        s.map("ADSB.Treeview",
              background=[("selected",SEL)], foreground=[("selected","#ffffff")])
        s.configure("Vertical.TScrollbar",   background=PANEL, troughcolor=BG, arrowcolor=MUTED)
        s.configure("Horizontal.TScrollbar", background=PANEL, troughcolor=BG, arrowcolor=MUTED)

    # ── Receiver ─────────────────────────────────────────────────────────
    def _start_receiver(self):
        self.receiver = ReceiverThread(self.decoder, self.update_q)
        self.receiver.start()

    def _start(self):
        if self.receiver and self.receiver.is_alive():
            self._log("Receiver already running.")
            return
        self._log("Starting receiver…")
        self.lbl_status.config(text="Starting…", fg=MUTED)
        self._start_receiver()

    def _stop(self):
        if self.receiver: self.receiver.stop()
        self._log("Receiver stopped.")
        self.lbl_status.config(text="⏹  Stopped", fg=ACCENT2)

    def _clear(self):
        self.decoder.aircraft.clear()
        self.decoder._cpr_cache.clear()
        for row in self.tree.get_children(): self.tree.delete(row)
        self._ac_seen.clear()
        self._msg_count = 0
        if self.map_widget:
            canvas = self.map_widget.canvas
            canvas.delete("ac_icon")
            canvas.delete("ac_label")
            for m in self._map_markers.values():
                try: m.delete()
                except Exception: pass
            for p in self._trail_paths.values():
                try: p.delete()
                except Exception: pass
        self._map_markers.clear()
        self._ac_trails.clear()
        self._trail_paths.clear()
        self._log("Cleared all data.")

    # ── Queue polling ─────────────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                kind, data = self.update_q.get_nowait()
                if kind == "status":
                    self.lbl_status.config(
                        text=data,
                        fg=ACCENT  if "Receiving" in data else
                           ACCENT2 if ("error" in data or "❌" in data) else MUTED)
                    self._log(data)
                elif kind == "log":
                    self._log(data)
                elif kind == "update":
                    self._refresh_row(data)
                    self._msg_count += 1
                    self.lbl_msgs.config(text=f"Messages: {self._msg_count}")
                    self.lbl_ac.config(text=f"Aircraft: {len(self.decoder.aircraft)}")
                    ac = self.decoder.aircraft.get(data)
                    if ac:
                        start_enrichment(ac, self.update_q)
                    if data == self._selected_icao:
                        self._update_detail(data)
                elif kind == "enrich":
                    self._refresh_row(data)
                    if data == self._selected_icao:
                        self._update_detail(data)
                        ac = self.decoder.aircraft.get(data, {})
                        self.lbl_enrich.config(text="✓ enriched")
                    ac = self.decoder.aircraft.get(data, {})
                    self._log(f"Enriched {data}: {ac.get('type','?')} / {ac.get('operator','?')}")
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ── Table ─────────────────────────────────────────────────────────────
    def _refresh_row(self, icao):
        ac = self.decoder.aircraft.get(icao)
        if not ac: return
        vals   = self._row(ac)
        is_new = icao not in self._ac_seen
        self._ac_seen.add(icao)
        ri  = list(self.decoder.aircraft.keys()).index(icao)
        tag = "new" if is_new else ("even" if ri%2==0 else "odd")
        tags = (tag, "partial") if is_partial(ac) else (tag,)
        if self.tree.exists(icao):
            self.tree.item(icao, values=vals, tags=tags)
        else:
            self.tree.insert("", "end", iid=icao, values=vals, tags=tags)
        if is_new:
            self.after(2000, lambda i=icao, r=ri:
                self.tree.item(i, tags=(("even" if r%2==0 else "odd"),) +
                               (("partial",) if is_partial(self.decoder.aircraft[i]) else ()))
                if self.tree.exists(i) and i in self.decoder.aircraft else None)

    @staticmethod
    def _row(ac):
        def f(v, fs=None):
            return "—" if v is None else (f"{v:{fs}}" if fs else str(v))
        ts = ac["last_seen"].strftime("%H:%M:%S") if ac["last_seen"] else "—"
        return (
            ac["icao"], ac["callsign"] or "—", ac.get("flight") or "—",
            ac.get("class") or "—",
            f(ac["altitude"], ",d"),
            f"{ac['lat']:.4f}" if ac["lat"] is not None else "—",
            f"{ac['lon']:.4f}" if ac["lon"] is not None else "—",
            f(ac["speed"]), f(ac["heading"]),
            f"{ac['vrate']:+,d}" if ac["vrate"] is not None else "—",
            ac["squawk"] or "—", str(ac["msgs"]), ts,
        )

    # ── Detail panel ──────────────────────────────────────────────────────
    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel: return
        icao = sel[0]
        prev = self._selected_icao
        self._selected_icao = icao
        self.lbl_enrich.config(text="")
        # Switching to a different aircraft — reset photo state
        if icao != prev:
            self._photo_icao         = None
            self._photo_loading_icao = None
            self._photo_img          = None
            self.lbl_photo.config(image="", text="…", compound="none")
        self._update_detail(icao)
        if self.map_widget:
            ac = self.decoder.aircraft.get(icao, {})
            if ac.get("lat") and ac.get("lon"):
                self.map_widget.set_position(ac["lat"], ac["lon"])
        ac = self.decoder.aircraft.get(icao, {})
        safe = {k: str(v) for k, v in ac.items() if not k.startswith("_")}
        self._log(f"Selected {icao}: " + " | ".join(f"{k}={v}" for k, v in safe.items() if v and v != "None"))

    def _update_detail(self, icao):
        ac = self.decoder.aircraft.get(icao)
        if not ac: return
        v = self._dvars
        v["icao"].set(ac["icao"])
        v["callsign"].set(ac["callsign"] or "—")
        v["flight"].set(ac.get("flight") or "—")
        v["reg"].set(ac.get("reg") or "—")
        v["type"].set(ac.get("type") or "—")
        v["class"].set(ac.get("class") or "—")
        v["manufacturer"].set(ac.get("manufacturer") or "—")
        v["operator"].set(ac.get("operator") or "—")
        v["route"].set(ac.get("route") or "—")
        v["squawk"].set(ac.get("squawk") or "—")
        alt = ac.get("altitude"); v["alt"].set(f"{alt:,} ft" if alt else "—")
        spd = ac.get("speed");   v["spd"].set(f"{spd} kt"   if spd else "—")
        hdg = ac.get("heading"); v["hdg"].set(f"{hdg}°"     if hdg else "—")
        self.lbl_hint.config(text=f"ICAO {icao}  •  {ac.get('msgs',0)} messages received")

        if not PIL_OK:
            self.lbl_photo.config(text="pip install\npillow\nfor photos", image="")
            return

        # Photo already showing for this aircraft — leave it completely alone
        if self._photo_icao == icao:
            return

        # Fetch already in-flight for this aircraft — don't re-trigger
        if self._photo_loading_icao == icao:
            return

        # Now we need to start a fetch (or show a status message)
        if ac.get("_enriched"):
            photo_url = ac.get("photo_url", "")
            if photo_url:
                self.lbl_photo.config(text="Loading…", image="", compound="none")
                self._photo_loading_icao = icao
                threading.Thread(target=self._fetch_photo,
                                 args=(photo_url, icao), daemon=True).start()
            else:
                self.lbl_photo.config(text="No photo\nfound", image="")
        else:
            self.lbl_photo.config(text="Fetching\ninfo…", image="")

    def _fetch_photo(self, url, icao):
        if not url:
            self._photo_loading_icao = None
            self.after(0, lambda: self.lbl_photo.config(text="No photo\nfound", image=""))
            return
        data = _http_bytes(url)
        if not data:
            self._photo_loading_icao = None
            self.after(0, lambda: self.lbl_photo.config(text="Photo\nunavailable", image=""))
            return
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((176, 106), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._photo_loading_icao = None
            if self._selected_icao == icao:
                self.after(0, lambda p=photo, i=icao: self._apply_photo(p, i))
        except Exception:
            self._photo_loading_icao = None
            self.after(0, lambda: self.lbl_photo.config(text="Photo\nerror", image=""))

    def _apply_photo(self, photo, icao):
        self._photo_img  = photo
        self._photo_icao = icao
        self.lbl_photo.config(image=photo, text="", compound="none")

    # ── Map ───────────────────────────────────────────────────────────────
    def _map_refresh_loop(self):
        self._update_markers()
        self.after(3000, self._map_refresh_loop)

    def _setup_map_hover(self):
        if not self.map_widget: return
        self._bind_canvas_hover()
        self._last_zoom = getattr(self.map_widget, "zoom", None)
        self._zoom_poll()

    def _bind_canvas_hover(self):
        try:
            c = self.map_widget.canvas
            c.bind("<Motion>", self._on_map_motion)
            c.bind("<Leave>",  lambda e: self._hide_tip())
        except Exception as e:
            self._log(f"Canvas bind error: {e}")

    def _zoom_poll(self):
        if not self.map_widget: return
        z = getattr(self.map_widget, "zoom", None)
        if z != self._last_zoom:
            self._last_zoom = z
            self._hide_tip()
            self._tooltip_icao = None
            self._bind_canvas_hover()
        self.after(300, self._zoom_poll)

    # ── Coordinate conversion ─────────────────────────────────────────────
    @staticmethod
    def _deg2num(lat: float, lon: float, zoom: float):
        """OSM tile coordinate math — zoom must be the same float the map uses."""
        n     = 2.0 ** zoom
        x     = (lon + 180.0) / 360.0 * n
        lat_r = math.radians(lat)
        y     = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
        return x, y

    def _geo_to_canvas(self, lat, lon):
        """Convert lat/lon to canvas pixels."""
        mw = self.map_widget
        try:
            zoom       = float(mw.zoom)          # keep full float precision
            tx, ty     = self._deg2num(lat, lon, zoom)
            ul_x, ul_y = mw.upper_left_tile_pos
            ts         = mw.tile_size
            return (tx - ul_x) * ts, (ty - ul_y) * ts
        except Exception as e:
            if not getattr(self, "_geo_err_logged", False):
                self._geo_err_logged = True
                self._log(f"[geo-dbg] math failed: {e}")
        return None

    # ── Plane icon cache (class-level so PhotoImages are never GC'd) ──────
    _ICON_CACHE: dict = {}

    def _plane_icon(self, heading: float, size=28):
        """
        White ✈-shaped plane icon pointing in the heading direction.
        Drawn as a filled polygon that mimics the ✈ glyph silhouette.
        """
        slot  = round(heading / 5) * 5 % 360
        key   = (slot, size)
        if key in App._ICON_CACHE:
            return App._ICON_CACHE[key]
        if not PIL_OK:
            return None
        from PIL import Image as PILImage, ImageDraw
        s  = size
        img = PILImage.new("RGBA", (s, s), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        # Plane pointing NORTH (up). Coordinates as fractions of size.
        # Body / fuselage centre line runs 0→1 top-to-bottom.
        # Wings span across at ~40 % down.
        def p(fx, fy): return (fx * s, fy * s)
        body = [
            p(0.50, 0.02),   # nose tip
            p(0.56, 0.30),   # right shoulder
            p(0.95, 0.48),   # right wingtip
            p(0.90, 0.56),   # right wing trailing edge
            p(0.58, 0.44),   # right wing root rear
            p(0.62, 0.80),   # right tailplane tip
            p(0.54, 0.76),   # right tail root
            p(0.50, 0.90),   # tail centre
            p(0.46, 0.76),   # left tail root
            p(0.38, 0.80),   # left tailplane tip
            p(0.42, 0.44),   # left wing root rear
            p(0.10, 0.56),   # left wing trailing edge
            p(0.05, 0.48),   # left wingtip
            p(0.44, 0.30),   # left shoulder
        ]
        d.polygon(body, fill="white", outline="black")
        # Rotate so nose points in heading direction
        # PIL rotate: positive = counter-clockwise; heading 0 = north = no rotation
        img   = img.rotate(-slot, resample=PILImage.BICUBIC, expand=False)
        photo = ImageTk.PhotoImage(img)
        App._ICON_CACHE[key] = photo
        return photo

    # ── Marker update ─────────────────────────────────────────────────────
    def _update_markers(self):
        if not self.map_widget: return

        for icao, ac in list(self.decoder.aircraft.items()):
            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is None or lon is None: continue

            hdg  = float(ac.get("heading") or 0)
            slot = round(hdg / 5) * 5 % 360
            icon = self._plane_icon(hdg)
            cs   = (ac.get("callsign") or icao)[:8]

            # ── Trail ──────────────────────────────────────────────
            trail = self._ac_trails.setdefault(icao, deque(maxlen=60))
            last  = trail[-1] if trail else None
            if last is None or abs(lat-last[0]) + abs(lon-last[1]) > 0.0002:
                trail.append((lat, lon))
            if len(trail) >= 2:
                ep = self._trail_paths.get(icao)
                try:
                    if ep: ep.delete()
                    self._trail_paths[icao] = self.map_widget.set_path(
                        list(trail), color="#00d4ff", width=2)
                except Exception:
                    pass

            # ── Marker: always delete+recreate for reliable repositioning ──
            existing = self._map_markers.get(icao)
            try:
                if existing:
                    existing.delete()
                kwargs = dict(
                    text=cs,
                    font=("Consolas", 7),
                    command=lambda ev, i=icao: self._marker_click(i),
                )
                if icon:
                    kwargs["icon"]        = icon
                    kwargs["icon_anchor"] = "center"
                else:
                    kwargs["marker_color_circle"]  = "#ffffff"
                    kwargs["marker_color_outside"] = "#333333"
                m = self.map_widget.set_marker(lat, lon, **kwargs)
                if icon:
                    try:
                        m.canvas_icon.image = icon
                    except Exception:
                        pass
                m._icon_ref = icon
                self._map_markers[icao] = m
            except Exception:
                pass

            # ── Store canvas xy for hover (computed fresh each cycle) ──────
            xy = self._geo_to_canvas(lat, lon)
            if xy:
                self._canvas_items[icao] = {"cx": xy[0], "cy": xy[1]}

    # ── Hover tooltip ─────────────────────────────────────────────────────
    def _on_map_motion(self, event):
        mouse_cx = event.x
        mouse_cy = event.y
        hit  = None
        best = 40

        canvas = self.map_widget.canvas if self.map_widget else None

        for icao, m in list(self._map_markers.items()):
            # Prefer reading the actual canvas item position — always accurate
            px = py = None
            if canvas:
                icon_item = m.__dict__.get("canvas_icon")
                if icon_item:
                    try:
                        coords = canvas.coords(icon_item)
                        if coords:
                            px, py = coords[0], coords[1]
                    except Exception:
                        pass
            # Fallback to computed geo→canvas
            if px is None:
                xy = self._geo_to_canvas(
                    self.decoder.aircraft.get(icao, {}).get("lat"),
                    self.decoder.aircraft.get(icao, {}).get("lon"),
                ) if self.decoder.aircraft.get(icao, {}).get("lat") else None
                if xy:
                    px, py = xy
            if px is None:
                # Last resort: use cached value
                cached = self._canvas_items.get(icao)
                if cached:
                    px, py = cached["cx"], cached["cy"]
            if px is None:
                continue

            self._canvas_items[icao] = {"cx": px, "cy": py}
            d = ((mouse_cx - px)**2 + (mouse_cy - py)**2) ** 0.5
            if d < best:
                best = d
                hit  = icao

        if hit:
            if self._tooltip_icao != hit:
                self._tooltip_icao = hit
                self._hide_tip()
                ac = self.decoder.aircraft.get(hit, {})
                def _fv(v, suffix=""):
                    return f"{v}{suffix}" if v not in (None, "", 0) else "—"
                lines = [
                    f"  ✈  {_fv(ac.get('callsign'))}  {_fv(ac.get('flight'))}   {_fv(ac.get('type'))}  ",
                    f"  Alt: {_fv(ac.get('altitude'),' ft')}  Spd: {_fv(ac.get('speed'),' kt')}  Hdg: {_fv(ac.get('heading'),'°')}  ",
                    f"  {_fv(ac.get('operator'))}  |  Reg: {_fv(ac.get('reg'))}  ",
                    f"  Route: {_fv(ac.get('route'))}  |  Squawk: {_fv(ac.get('squawk'))}  ",
                ]
                self._show_tip_at(event.x_root, event.y_root, "\n".join(lines))
        else:
            if self._tooltip_icao is not None:
                self._tooltip_icao = None
                self._hide_tip()

    def _show_tip_at(self, root_x, root_y, text):
        self._hide_tip()
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.wm_geometry("+0+0")
        outer = tk.Frame(tip, bg=ACCENT, bd=1)
        outer.pack()
        tk.Label(outer, text=text, justify="left",
                 bg="#0a0e1a", fg=FG, font=("Consolas", 9),
                 padx=8, pady=5).pack()
        tip.update_idletasks()
        tw = tip.winfo_reqwidth()
        th = tip.winfo_reqheight()
        if self.map_widget:
            mx = self.map_widget.winfo_rootx()
            my = self.map_widget.winfo_rooty()
            mw = self.map_widget.winfo_width()
            mh = self.map_widget.winfo_height()
            tx = root_x + 16
            ty = root_y + 10
            if tx + tw > mx + mw - 4: tx = root_x - tw - 8
            if ty + th > my + mh - 4: ty = root_y - th - 8
            tx = max(mx, min(tx, mx + mw - tw - 2))
            ty = max(my, min(ty, my + mh - th - 2))
        else:
            tx, ty = root_x + 16, root_y + 10
        tip.wm_geometry(f"+{tx}+{ty}")
        self._tooltip_win = tip

    def _hide_tip(self):
        if self._tooltip_win:
            try:  self._tooltip_win.destroy()
            except Exception: pass
            self._tooltip_win = None

    def _marker_click(self, icao):
        if self.tree.exists(icao):
            self.tree.selection_set(icao)
            self.tree.see(icao)
        self._selected_icao = icao
        self._update_detail(icao)

    # ── Log ───────────────────────────────────────────────────────────────
    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.config(state="normal")
        self.log.insert("end", f"[{ts}]  {msg}\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def on_close(self):
        self._stop()
        self.destroy()

# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    missing = []
    if not MAP_OK: missing.append("tkintermapview  ← live map")
    if not PIL_OK: missing.append("pillow          ← aircraft photos")
    if missing:
        print("Tip — install missing optional packages:")
        for m in missing: print(f"  pip install {m}")
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()