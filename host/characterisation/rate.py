#!/usr/bin/env python3
"""rate.py - what sample rate does the link actually sustain?

Over the UART console this topped out near 12.8 Hz because a 90-byte frame at
115200 baud plus the round trip capped it.  Over the board's own USB the
console stops being the limit, so this sweeps the chip's own trigger period
and reports what arrives.
"""
import statistics, sys, time
from pcap04 import PCap04

CASES = [(32, 2000), (32, 800), (16, 400), (8, 200), (8, 100), (4, 50)]

d = PCap04()
print("transport: %s  (%s)\n" % (d.port_name, "board USB-C" if d.via_usb else "UART via Debug Probe"))
d.power(True); d.load('f')
print("%6s %8s %10s %10s %10s %9s" % ("C_AVRG", "CONV_T", "nominal", "achieved", "sd/ppm", "pF"))
for avrg, conv in CASES:
    d.set_avrg(avrg); d.set_conv(conv)
    time.sleep(0.5)
    try:
        s = d.collect(60, timeout=20.0)
        st = d.stats(s, 2)
        cref = d.cal.cref_pf() if (d.cal and d.cal.cref_pf()) else 1.0
        print("%6d %8d %9.1f %9.1f %10.1f %9.4f"
              % (avrg, conv, 51000.0/(2*conv), st["rate_hz"], st["ppm"], st["mean"]*cref))
    except Exception as e:
        print("%6d %8d   %s" % (avrg, conv, e))
d.set_avrg(32); d.set_conv(2000)
d.close()
