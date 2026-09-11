#!/usr/bin/env python3
"""disch.py - find the DISCHARGE_TIME the level probe needs, and what it costs.

DISCHARGE_TIME lives in cfg 0x0C (7:0) and cfg 0x0D (1:0); cfg 0x0D also holds
C_TRIG_SEL, which must stay at 2 (timer triggered), hence the |0x08.
"""
import statistics, time
from pcap04 import PCap04

d = PCap04(); d.power(True); d.load('f'); time.sleep(0.3)
cref = d.cal.cref_pf() if (d.cal and d.cal.cref_pf()) else 40.14
print("Cref = %.3f pF\n" % cref)
print("%10s %9s %10s %10s %9s %8s  %s" %
      ("DISCHARGE", "rate/Hz", "ch1/pF", "ch2/pF", "ch1 ppm", "st2", "note"))

for dt in (2, 4, 6, 8, 10, 12, 16, 24):
    d.cmd("wr 0C %02X" % (dt & 0xFF))
    d.cmd("wr 0D %02X" % (((dt >> 8) & 3) | 0x08))
    d.cmd("init"); d.cmd("start"); time.sleep(0.7)
    try:
        s = d.collect(20, timeout=12.0)
    except Exception as e:
        print("%10d   %s" % (dt, str(e)[:50])); continue
    if len(s) < 5:
        print("%10d   only %d frames" % (dt, len(s))); continue
    v1 = [x.channels[1] for x in s]
    v2 = [x.channels[2] for x in s]
    st2 = s[-1].status[2]
    err = ",".join("PC%d" % k for k in range(6) if st2 & (1 << k)) or "ok"
    try:
        rate = d.stats(s, 2)["rate_hz"]
    except Exception:
        rate = float("nan")
    m1 = statistics.mean(v1); sd1 = statistics.stdev(v1)
    print("%10d %9.2f %10.4f %10.4f %9.0f %3X  %s"
          % (dt, rate, m1 * cref, statistics.mean(v2) * cref, sd1 / m1 * 1e6, st2, err))

d.load('f'); d.close()
