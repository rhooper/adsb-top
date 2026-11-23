#!/usr/bin/env python3
"""
Check adsb-mon's decoder against synthetic IQ built from known Mode S frames,
since the dev dongle hears no aircraft (see CLAUDE.md).

2026-11-18.  Usage: python3 scripts/claude/synth_check.py   (exits non-zero on failure)
"""

import importlib.util
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location(
    "adsb_mon", Path(__file__).resolve().parents[2] / "adsb-mon.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def iq(frames):
    """Unsigned 8-bit interleaved IQ at 2 MS/s: preamble pulses at 0/2/7/9, PPM data from 16."""
    mags = []
    for hexmsg in frames:
        bits = bin(int(hexmsg, 16))[2:].zfill(len(hexmsg) * 4)
        frame = np.zeros(16 + 2 * len(bits))
        frame[[0, 2, 7, 9]] = 1
        for i, b in enumerate(bits):
            frame[16 + 2 * i + (0 if b == "1" else 1)] = 1
        mags += [np.zeros(300), frame]
    mag = np.concatenate(mags + [np.zeros(300)]) * 100
    out = np.empty(2 * len(mag), dtype=np.uint8)
    out[0::2] = np.clip(127.5 + mag, 0, 255)
    out[1::2] = 127
    return out


def with_crc(body_hex):
    body = bytes.fromhex(body_hex)
    return (body + m._crc24(body).to_bytes(3, "big")).hex().upper()


def identity_reply(df, addr, squawk, extra=""):
    """DF5 (56 bit) or DF21 (112 bit, `extra` = 7-byte MB field) with AP = CRC ^ address."""
    a, b, c, d = (int(ch) for ch in squawk)
    order = [(c, 0), (a, 0), (c, 1), (a, 1), (c, 2), (a, 2), None,
             (b, 0), (d, 0), (b, 1), (d, 1), (b, 2), (d, 2)]
    idf = 0
    for slot in order:
        idf = idf << 1 | (0 if slot is None else (slot[0] >> slot[1]) & 1)
    body = bytes([df << 3, 0, idf >> 8, idf & 0xFF]) + bytes.fromhex(extra)
    ap = m._crc24(body) ^ int(addr, 16)
    return (body + ap.to_bytes(3, "big")).hex().upper()


def all_call(addr, iid=0):
    """DF11 all-call reply: parity = CRC ^ interrogator ID."""
    body = bytes([11 << 3 | 5]) + bytes.fromhex(addr)
    return (body + (m._crc24(body) ^ iid).to_bytes(3, "big")).hex().upper()


def altitude_reply(df, addr, ac13, extra=""):
    """DF4 (56 bit) or DF20 (112 bit) with a raw 13-bit altitude code."""
    body = bytes([df << 3, 0, ac13 >> 8, ac13 & 0xFF]) + bytes.fromhex(extra)
    return (body + (m._crc24(body) ^ int(addr, 16)).to_bytes(3, "big")).hex().upper()


def ident(addr, cs, df=17, cf=5, tc=4, cat=0):
    """Identification squitter; `cf` is CA for DF17, CF for DF18. '#' in `cs` = code 0."""
    codes = [0 if ch == "#" else 32 if ch == " " else m.CS_CHARSET.index(ch) for ch in cs.ljust(8)]
    chars = sum(c << (42 - 6 * i) for i, c in enumerate(codes))
    return with_crc(f"{df << 3 | cf:02X}{addr}{tc << 3 | cat:02X}{chars:012X}")


failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


dec = m.ADSBDecoder()
check("squawk of pyModeS vector 2A00516D492B80",
      dec._decode_squawk(bytes.fromhex("2A00516D492B80")), "0356")

# An identity reply for an unheard address must be dropped as possible noise.
lone = identity_reply(5, "ABCDEF", "1200")
check("DF5 from unknown address dropped", dec.demodulate(iq([lone])), [])

adsb = [
    "8D4840D6202CC371C32CE0576098",                       # KLM1023
    "8D40621D58C382D690C8AC2863A7", "8D40621D58C386435CC412692AD6",
    "8D485020994409940838175B284F",                       # 159 kt, 182.9°, −832 fpm
    with_crc("8DABC123272CC371C32CE0"),                    # KLM1023's ident as category A7
]
# Identity replies only count once their address is known, so a later batch.
replies = [identity_reply(5, "4840D6", "7700"),
           identity_reply(21, "40621D", "4521", "00000000000000")]
for batch in (adsb, replies):
    for msg in dec.demodulate(iq(batch)):
        dec.update(msg)
ac = dec.aircraft
check("aircraft heard", sorted(ac), ["40621D", "4840D6", "485020", "ABC123"])
check("KLM1023 callsign", ac["4840D6"]["callsign"], "KLM1023")
check("no category when CA is 0", ac["4840D6"]["category"], "")
check("category A7", ac["ABC123"]["category"], "A7")
check("DF5 squawk", ac["4840D6"]["squawk"], "7700")
check("DF21 squawk", ac["40621D"]["squawk"], "4521")
check("altitude", ac["40621D"]["altitude"], 38000)
check("position", (round(ac["40621D"]["lat"], 4), round(ac["40621D"]["lon"], 4)), (52.2658, 3.9389))
check("velocity", (ac["485020"]["speed"], ac["485020"]["heading"], ac["485020"]["vrate"]),
      (159, 182.9, -832))

# Mode S only (no ADS-B): a clean DF11 introduces the address, then altitude
# and identity replies fill it in.
modes = m.ADSBDecoder()
noise = [all_call("C0FFEE", iid=5), altitude_reply(4, "C0FFEE", 0x14B4), identity_reply(5, "C0FFEE", "1200")]
check("unknown address: DF11 with IID, DF4, DF5 all dropped", modes.demodulate(iq(noise)), [])
for batch in ([all_call("C0FFEE")],
              [altitude_reply(4, "C0FFEE", 0x414), identity_reply(5, "C0FFEE", "1200"),
               all_call("C0FFEE", iid=5)]):
    for msg in modes.demodulate(iq(batch)):
        modes.update(msg)
check("Mode S-only aircraft heard", sorted(modes.aircraft), ["C0FFEE"])
c0 = modes.aircraft["C0FFEE"]
check("Mode S-only: altitude (Q bit, 25 ft)", c0["altitude"], 5500)
check("Mode S-only: squawk", c0["squawk"], "1200")
check("Mode S-only: messages incl. DF11 with IID", c0["msgs"], 4)
check("Mode S-only shows as partial", m.is_partial(c0), True)
modes.update(bytes.fromhex(altitude_reply(20, "C0FFEE", 0x14B4, "00000000000000")))
check("DF20 altitude (pyModeS vector A02014B4…)", c0["altitude"], 32300)
check("Gillham (100 ft) altitude, code from pyModeS", m.ADSBDecoder._decode_ac13(0x6A2), 10000)

calls = m.ADSBDecoder()
for msg in calls.demodulate(iq([ident("A1B2C3", "AAL1234X"), ident("A1B2C4", "########")])):
    calls.update(msg)
check("8-character callsign keeps its last character", calls.aircraft["A1B2C3"]["callsign"], "AAL1234X")
check("all-zero callsign (no identity) is dropped", "A1B2C4" in calls.aircraft, False)

# DF18: the CF field says whether the address is an ICAO one. Position ME from
# 8D40621D58C382D690C8AC; its low bit of byte 0 is the TIS-B/ADS-R IMF flag.
tisb = m.ADSBDecoder()
pos_me = "C382D690C8AC"
for cf, addr, me0 in ((2, "A00001", "58"), (2, "0E0002", "59"), (6, "0E0003", "59"),
                      (1, "0E0004", "58"), (5, "0E0005", "58"), (3, "A00006", "58")):
    for msg in tisb.demodulate(iq([with_crc(f"{18 << 3 | cf:02X}{addr}{me0}{pos_me}")])):
        tisb.update(msg)
check("DF18 addresses by CF/IMF", sorted(tisb.aircraft),
      ["A00001", "~0E0002", "~0E0003", "~0E0004", "~0E0005"])
check("TIS-B altitude still decoded", tisb.aircraft["~0E0002"]["altitude"], 38000)
db = tisb.db
db.records, db.mil_ranges, db.ready = {"0E0002": "N1;B738;10;BOEING;;NAVY;"}, [(0x0E0000, 0x0EFFFF)], True
anon = tisb.aircraft["~0E0002"]
db.classify(anon)
check("track number not looked up or called military", (anon["reg"], anon["class"]), ("", ""))

# tar1090-db beats hexdb.io, whichever answers first. Lookups are stubbed offline.
import queue
STEARMAN = {"ICAOTypeCode": "ST75", "Registration": "N54561", "Type": "Stearman A75N1",
            "Manufacturer": "Boeing", "RegisteredOwners": "Private"}
m._http_json = lambda url, timeout=5: STEARMAN if "hexdb" in url else {}
q = queue.Queue()


def fresh_db():
    d = m.AircraftDB()
    d.records = {"A6EDC3": "N54561;A21N;00;AIRBUS A-321neo;2026;WILMINGTON TRUST CO TRUSTEE;",
                 "AE1253": "62-4126;R135;10;Boeing RC-135W Rivet Joint;;;"}
    d.types, d.ready = {}, True
    return d


for order in ("db first", "hexdb first"):
    d = fresh_db()
    plane = {"icao": "A6EDC3", "callsign": "UAL745", "category": "", "reg": "", "type": "",
             "manufacturer": "", "operator": "", "_enriched": False, "_enriching": False}
    steps = [lambda: d.classify(plane), lambda: m.enrich_aircraft(plane, q)]
    for step in (steps if order == "db first" else steps[::-1]):
        step()
    check(f"reused registration, {order}",
          (plane["type"], plane["manufacturer"], plane["operator"]),
          ("AIRBUS A-321neo", "", "WILMINGTON TRUST CO TRUSTEE"))
d = fresh_db()
rj = {"icao": "AE1253", "callsign": "", "category": "", "reg": "", "type": "",
      "manufacturer": "", "operator": "", "_enriched": False, "_enriching": False}
d.classify(rj)
STEARMAN["RegisteredOwners"] = "United States Air Force"
m.enrich_aircraft(rj, q)
check("hexdb.io fills an operator tar1090-db lacks", (rj["type"], rj["operator"]),
      ("Boeing RC-135W Rivet Joint", "United States Air Force"))

raise SystemExit(1 if failures else 0)
