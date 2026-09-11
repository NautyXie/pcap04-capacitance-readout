#!/usr/bin/env python3
"""range.py - what is the full-scale measurement range of this board?

The chip returns ratio = C / Cref in Q5.27, so full scale is 32 * Cref and the
LSB is Cref / 2**27.  Both ends therefore move with C_REF_SEL.  Sweep it
against the known 100 pF on ch2 to measure Cref at every code, and report the
range that each code buys.
"""
import statistics, time
from pcap04 import PCap04

KNOWN = 100.0
d = PCap04()
print("transport %s\n" % d.port_name)
d.power(True); d.load('f')
print("%3s %10s %12s %12s %11s" % ("N", "Cref/pF", "full scale", "LSB/aF", "100pF ratio"))
rows = []
for n in [0, 1, 2, 4, 8, 16, 24, 31]:
    d.set_refsel(n); time.sleep(0.4)
    try:
        r = statistics.mean(x.channels[2] for x in d.collect(8))
    except Exception as e:
        print("%3d  %s" % (n, e)); continue
    cref = KNOWN / r
    rows.append((n, cref, r))
    print("%3d %10.3f %9.0f pF %10.2f %11.4f" % (n, cref, 32.0 * cref, cref / 2**27 * 1e6, r))
d.set_refsel(31)
d.close()
if len(rows) > 1:
    lo = min(rows, key=lambda x: x[1]); hi = max(rows, key=lambda x: x[1])
    print("\ninternal reference spans %.2f .. %.2f pF" % (lo[1], hi[1]))
    print("so digital full scale spans %.0f .. %.0f pF" % (32*lo[1], 32*hi[1]))
