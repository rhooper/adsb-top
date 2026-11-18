# adsb-mon

Tk app `adsb-mon.py`, plus Textual terminal view `adsb-top.py`. See README.md for setup.

- `adsb-top.py` loads `adsb-mon.py` via importlib and uses `ADSBDecoder`,
  `ReceiverThread`, `start_enrichment`, `is_partial`, the aircraft dict keys and the
  `("status" | "log" | "update" | "enrich", data)` queue protocol — changing any of
  those breaks both. "status" replaces the status bar; "log" only goes to the log pane.

- `adsb-mon.py` has CRLF line endings; scripted edits must keep them (`newline=""`
  in Python) or the diff rewrites every line.

- The dongle on the dev Mac (RTL2838/R820T) currently hears no aircraft (`rtl_adsb`
  shows only noise), so a live run proves the SDR pipeline, not the decoder. Check
  decoder changes against synthetic IQ built from known DF17 frames, e.g.
  `8D4840D6202CC371C32CE0576098` (KLM1023), `8D40621D58C382D690C8AC2863A7` +
  `8D40621D58C386435CC412692AD6` (38000 ft, 52.2658/3.9389),
  `8D485020994409940838175B284F` (159 kt, 182.9°, −832 fpm). At 2 MS/s: preamble
  pulses at samples 0/2/7/9, data from sample 16, 2 samples per bit (PPM).
  `scripts/claude/synth_check.py` does this; extend it for decoder changes.
- Neither textual nor the GUI deps are installed globally; run the apps with
  `uv run --with textual --with numpy` (adsb-top) or `--with numpy --with pillow
  --with tkintermapview` (adsb-mon).
- Only one process can hold the dongle; close `rtl_adsb`/SDR++ before running
  (`usb_claim_interface error -3` otherwise, briefly also right after one exits).
