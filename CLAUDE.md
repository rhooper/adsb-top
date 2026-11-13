# adsb-mon

Single-file Tk app: `adsb-mon.py`. See README.md for setup.

- The dongle on the dev Mac (RTL2838/R820T) currently hears no aircraft (`rtl_adsb`
  shows only noise), so a live run proves the SDR pipeline, not the decoder. Check
  decoder changes against synthetic IQ built from known DF17 frames, e.g.
  `8D4840D6202CC371C32CE0576098` (KLM1023), `8D40621D58C382D690C8AC2863A7` +
  `8D40621D58C386435CC412692AD6` (38000 ft, 52.2658/3.9389),
  `8D485020994409940838175B284F` (159 kt, 182.9°, −832 fpm). At 2 MS/s: preamble
  pulses at samples 0/2/7/9, data from sample 16, 2 samples per bit (PPM).
- Only one process can hold the dongle; close `rtl_adsb`/SDR++ before running
  (`usb_claim_interface error -3` otherwise, briefly also right after one exits).
