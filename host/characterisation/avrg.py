#!/usr/bin/env python3
"""avrg.py - noise of RES2 (the 100 pF on CTEST1) vs the CDC averaging depth.

Two traps this script avoids:

  * Polling faster than the conversion rate returns the SAME result register
    contents over and over, and the sample standard deviation collapses to
    exactly zero.  Every reading here is de-duplicated against the previous
    raw 32-bit word, and the achieved update rate is measured rather than
    computed from a nominal f_OLF.
  * C_AVRG is split across cfg 07 (7:0) and cfg 08 (12:8).  The 256/257 pair
    below tests whether a zero low byte is mis-handled, which the first pass
    of this sweep suggested.

  CONV_TIME : cfg 09 (7:0), cfg 0A (15:8), cfg 0B (22:16)   t = 2*CT/f_OLF
"""
import re, sys, time, statistics
from session import open_port, cmd

CREF31 = 41.32          # pF, from the calib.py sweep at C_REF_SEL = 31
NEED = 12               # distinct conversions per point
CASES = [(32, 2000), (64, 2000), (128, 2000), (255, 2000),
         (128, 8000), (255, 8000), (256, 8000), (257, 8000),
         (1023, 8000), (1023, 32000), (4095, 32000)]


def resd(s):
    out = cmd(s, "resd")
    m = re.search(r"RES2 = 0x([0-9A-F]{8})", out)
    st = re.search(r"status 0x20\.\.0x22 = ([0-9A-F]{2}) ([0-9A-F]{2})", out)
    return (int(m.group(1), 16) if m else None,
            (int(st.group(1), 16), int(st.group(2), 16)) if st else (0, 0))


def collect(s, need, budget=25.0):
    """Read until `need` distinct conversions have arrived."""
    raws, flags, last, t0, npoll = [], set(), None, time.time(), 0
    while len(raws) < need and time.time() - t0 < budget:
        raw, (s0, s1) = resd(s)
        npoll += 1
        if not (s0 & 0x01): flags.add("RESET")
        if s0 & 0x20:       flags.add("CDC/DSP-COLL")
        if s0 & 0x80:       flags.add("wdog-POR")
        if s1 & 0x0F:       flags.add("ERR%X" % (s1 & 0x0F))
        if raw is None or raw == last:
            continue                    # same conversion, not a new sample
        last = raw
        raws.append((time.time(), raw))
    return raws, flags, npoll


s = open_port()
print("%6s %7s %5s %7s %13s %11s %9s %9s %8s  %s"
      % ("C_AVRG", "CONV_T", "n", "rate/Hz", "mean ratio", "sd", "sd/mean", "noise/aF", "C/pF", "flags"))

for avrg, ct in CASES:
    cmd(s, "loadf")
    cmd(s, "wr 07 %02X" % (avrg & 0xFF))
    cmd(s, "wr 08 %02X" % ((avrg >> 8) & 0x1F))
    cmd(s, "wr 09 %02X" % (ct & 0xFF))
    cmd(s, "wr 0A %02X" % ((ct >> 8) & 0xFF))
    cmd(s, "wr 0B %02X" % ((ct >> 16) & 0x7F))
    cmd(s, "init")
    cmd(s, "start")
    time.sleep(1.0)
    raws, flags, npoll = collect(s, NEED)
    if len(raws) < 4:
        print("%6d %7d  --- only %d distinct results in %d polls   %s"
              % (avrg, ct, len(raws), npoll, ",".join(sorted(flags)) or "-"))
        continue
    v = [r / 2.0 ** 27 for _, r in raws]
    span = raws[-1][0] - raws[0][0]
    rate = (len(raws) - 1) / span if span > 0 else float("nan")
    m, sd = statistics.mean(v), statistics.stdev(v)
    print("%6d %7d %5d %7.2f %13.6f %11.3e %7.1f pm %9.0f %8.3f  %s"
          % (avrg, ct, len(v), rate, m, sd, sd / m * 1e6, sd * CREF31 * 1e6,
             m * CREF31, ",".join(sorted(flags)) or "-"))
s.close()
print("\ndatasheet, floating + full compensation:  19 aF @ 10 Hz,  8 aF @ 2.5 Hz  (10 pF base)")
print("note: the measured rate is capped by the console round trip (~3 Hz), not by the chip,")
print("      so a point whose rate sits at ~3 Hz may be converting faster than shown.")
