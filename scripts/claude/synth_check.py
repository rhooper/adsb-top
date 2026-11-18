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

raise SystemExit(1 if failures else 0)
