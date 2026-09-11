#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
pcap.py - command line for the PCAP04_Acquisition_3CH v1.5 board.

    ./pcap.py power on              U5 load switch -> +3V3P
    ./pcap.py read                  one conversion, all channels
    ./pcap.py read -m g             six grounded electrodes instead
    ./pcap.py stream -n 50          live, at the chip's own rate
    ./pcap.py log run.csv -t 60     60 s to CSV with running statistics
    ./pcap.py calib 100 -c 2        calibrate against 100 pF on channel 2
    ./pcap.py calib 100 -c 2 --sweep    ... and characterise the whole
                                        reference bank while you are at it
    ./pcap.py speed precise         C_AVRG / CONV_TIME preset
    ./pcap.py health                status flags, rail, INTN
    ./pcap.py config                current front-end settings
    ./pcap.py console "wr 04 B1"    raw firmware command

Channel numbering
    floating (default)  ch0 = PC0/PC1 = J2/J3
                        ch1 = PC2/PC3 = J4/J5
                        ch2 = PC4/PC5 = J6/J7   (CTEST1 sits here)
    grounded            ch0..ch5 = PC0..PC5 = J2..J7
"""

import argparse
import csv
import math
import statistics
import sys
import time

from pcap04 import (PCap04, Calibration, PCapError, SPEED_PRESETS,
                    FLOATING_CHANNELS, GROUNDED_CHANNELS, C_AVRG_MAX)


# ---------------------------------------------------------------- helpers

def open_dev(a):
    dev = PCap04(port=a.port, verbose=a.verbose)
    if getattr(a, "mode", None):
        want = dev._norm_mode(a.mode)
        if getattr(a, "reload", False) or not dev.params().get("mode"):
            dev.load(want)
        else:
            dev.mode(want)
    return dev


def chan_label(dev, i):
    table = FLOATING_CHANNELS if dev.mode() == "f" else GROUNDED_CHANNELS
    port, conn = table.get(i, ("?", "?"))
    return "ch%d %-7s %-5s" % (i, port, conn)


def fmt_sample(dev, s, show_pf=True):
    out = []
    pf = s.pf if show_pf else [None] * s.n_channels
    for i, r in enumerate(s.channels):
        if s.is_open(i):
            out.append("%s  --- open ---" % chan_label(dev, i))
        elif pf[i] is not None:
            out.append("%s  %10.4f pF   (ratio %.6f)" % (chan_label(dev, i), pf[i], r))
        else:
            out.append("%s  ratio %.6f" % (chan_label(dev, i), r))
    if s.errors:
        out.append("  !! " + ", ".join(s.errors))
    return "\n".join(out)


def need_cal(dev):
    if dev.cal is None or dev.cal.cref_pf() is None:
        sys.stderr.write("no calibration yet - showing raw ratios.  Run\n"
                         "  ./pcap.py calib <known pF> -c <channel>\n"
                         "with a known capacitor fitted to get picofarads.\n\n")
        return False
    return True


# ---------------------------------------------------------------- commands

def cmd_power(a):
    with open_dev(a) as dev:
        if a.state == "cycle":
            print(dev.power_cycle())
        else:
            print(dev.power(a.state == "on"))
        if a.state != "off":
            mv = dev.rail_mv()
            if mv is not None:
                print("  +3V3P = %.3f V" % (mv / 1000.0))


def cmd_mode(a):
    with open_dev(a) as dev:
        if a.to is None:
            print("mode = %s" % ("floating" if dev.mode() == "f" else "grounded"))
        else:
            print(dev.mode(a.to))


def cmd_read(a):
    with open_dev(a) as dev:
        cal_ok = need_cal(dev)
        for _ in range(a.count):
            print(fmt_sample(dev, dev.read(), cal_ok))
            if a.count > 1:
                print()


def cmd_stream(a):
    with open_dev(a) as dev:
        cal_ok = need_cal(dev)
        hdr = "%-10s" % "t/s" + "".join("%14s" % ("ch%d/pF" % i if cal_ok else "ch%d" % i)
                                        for i in range(3 if dev.mode() == "f" else 6))
        print(hdr)
        t0 = None
        for s in dev.stream(a.number):
            if t0 is None:
                t0 = s.t_ms
            vals = s.pf if cal_ok else s.channels
            row = "%-10.3f" % ((s.t_ms - t0) / 1000.0)
            for i, v in enumerate(vals):
                row += "%14s" % ("open" if s.is_open(i)
                                 else ("%.4f" % v if v is not None else "?"))
            if s.errors:
                row += "   !! " + ",".join(s.errors)
            print(row)


def cmd_log(a):
    with open_dev(a) as dev:
        cal_ok = need_cal(dev)
        nch = 3 if dev.mode() == "f" else 6
        acc = [[] for _ in range(nch)]
        t_end = time.time() + a.seconds if a.seconds else None
        n_target = a.number or 0
        with open(a.path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["host_time", "board_t_ms"]
                       + ["ch%d_raw" % i for i in range(nch)]
                       + ["ch%d_ratio" % i for i in range(nch)]
                       + ["ch%d_pF" % i for i in range(nch)]
                       + ["status0", "status1", "status2", "errors"])
            n = 0
            t_report = time.time()
            try:
                for s in dev.stream(n_target):
                    pf = s.pf
                    w.writerow(["%.6f" % s.host_t, s.t_ms]
                               + [s.raw[i] for i in range(nch)]
                               + ["%.9f" % s.channels[i] for i in range(nch)]
                               + ["%.6f" % pf[i] if pf[i] is not None else ""
                                  for i in range(nch)]
                               + ["%02X" % v for v in s.status]
                               + ["|".join(s.errors)])
                    for i in range(nch):
                        if not s.is_open(i):
                            acc[i].append(s.channels[i])
                    n += 1
                    if time.time() - t_report > 2.0:
                        t_report = time.time()
                        sys.stdout.write("\r  %d samples  " % n)
                        for i in range(nch):
                            if len(acc[i]) > 2:
                                m, sd = statistics.mean(acc[i]), statistics.stdev(acc[i])
                                scale = dev.cal.cref_pf() if cal_ok else 1.0
                                sys.stdout.write("ch%d %.4f%s +/-%.1f ppm  "
                                                 % (i, m * scale, " pF" if cal_ok else "",
                                                    sd / m * 1e6))
                        sys.stdout.flush()
                    if t_end and time.time() > t_end:
                        break
            except KeyboardInterrupt:
                pass
        print("\n\nwrote %d samples to %s" % (n, a.path))
        for i in range(nch):
            v = acc[i]
            if len(v) < 3:
                continue
            m, sd = statistics.mean(v), statistics.stdev(v)
            scale = dev.cal.cref_pf() if cal_ok else 1.0
            drift = v[-1] - v[0]
            print("  %s  mean %.6f%s  sd %.3g (%.1f ppm)  drift %+.3g  n=%d"
                  % (chan_label(dev, i), m * scale, " pF" if cal_ok else "",
                     sd * scale, sd / m * 1e6, drift * scale, len(v)))


def cmd_calib(a):
    with open_dev(a) as dev:
        print("calibrating channel %d against %.4f pF (%s mode)%s ..."
              % (a.channel, a.known, "floating" if dev.mode() == "f" else "grounded",
                 ", sweeping every C_REF_SEL code" if a.sweep else ""))
        cal = dev.calibrate(a.known, channel=a.channel, refsel=a.refsel,
                            n=a.samples, sweep=a.sweep)
        path = cal.save(a.out)
        print("\nsaved to %s" % path)
        if a.sweep:
            print("\n%4s %12s %14s %10s" % ("N", "Cref/pF", "datasheet/pF", "excess"))
            codes = sorted(cal.cref)
            for c in codes:
                ds = 0.959 * c + 3.23
                print("%4d %12.3f %14.3f %+10.3f" % (c, cal.cref[c], ds, cal.cref[c] - ds))
            if len(codes) > 1:
                step = ((cal.cref[codes[-1]] - cal.cref[codes[0]])
                        / (codes[-1] - codes[0]))
                print("\n  mean step %.4f pF/code (datasheet 0.959)" % step)
                print("  extrapolated Cref(0) = %.3f pF (datasheet 3.23)"
                      % (cal.cref[codes[0]] - step * codes[0]))
                print("  the step width varies between codes, so absolute work must")
                print("  use the measured value at the code you actually run.")
        else:
            print("  Cref(N=%d) = %.4f pF" % (cal.refsel, cal.cref_pf()))
        for ch, st in cal.stray.items():
            print("  ch%d trace-to-ground parasitic (removed by C_COMP_EXT) = %.3f pF"
                  % (ch, st))


def cmd_speed(a):
    with open_dev(a) as dev:
        if a.preset:
            print(dev.speed(a.preset))
        print("\nmeasuring the achieved rate and noise ...")
        s = dev.collect(a.samples)
        for i in range(3 if dev.mode() == "f" else 6):
            try:
                st = dev.stats(s, i)
            except PCapError as e:
                print("  %s  %s" % (chan_label(dev, i), e)); continue
            scale = dev.cal.cref_pf() if (dev.cal and dev.cal.cref_pf()) else None
            noise = ("%.2f fF" % (st["sd"] * scale * 1000)) if scale else "-"
            print("  %s  rate %5.2f Hz   sd %8.1f ppm   noise %s"
                  % (chan_label(dev, i), st["rate_hz"], st["ppm"], noise))


def cmd_health(a):
    with open_dev(a) as dev:
        print(dev.health()["raw"])


def cmd_config(a):
    with open_dev(a) as dev:
        changed = False
        if a.avrg is not None:
            print(dev.set_avrg(a.avrg)); changed = True
        if a.conv is not None:
            print(dev.set_conv(a.conv)); changed = True
        if a.refsel is not None:
            print(dev.set_refsel(a.refsel)); changed = True
        if a.disch is not None:
            print(dev.set_discharge(a.disch)); changed = True
        if a.comp is not None:
            print(dev.set_comp("i" in a.comp, "e" in a.comp)); changed = True
        if a.ports is not None:
            print(dev.set_ports(int(a.ports, 0))); changed = True
        if changed:
            print()
        print(dev.params()["raw"])


def cmd_console(a):
    with open_dev(a) as dev:
        for line in a.command:
            print(dev.cmd(line))


# ---------------------------------------------------------------- parser

def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-p", "--port", help="serial port (default: first usbmodem)")
    p.add_argument("-v", "--verbose", action="store_true", help="echo the console traffic")
    p.add_argument("-m", "--mode", choices=["f", "g", "floating", "grounded"],
                   help="floating (3 pairs) or grounded (6 electrodes)")
    p.add_argument("--reload", action="store_true",
                   help="force a full firmware upload to the PCAP04 instead of a "
                        "runtime mode switch")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("power", help="U5 load switch for the PCAP04 supply")
    s.add_argument("state", choices=["on", "off", "cycle"])
    s.set_defaults(func=cmd_power)

    s = sub.add_parser("mode", help="query or switch grounded/floating")
    s.add_argument("to", nargs="?", choices=["f", "g", "floating", "grounded"])
    s.set_defaults(func=cmd_mode)

    s = sub.add_parser("read", help="one conversion")
    s.add_argument("-n", "--count", type=int, default=1)
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("stream", help="continuous, at the chip's conversion rate")
    s.add_argument("-n", "--number", type=int, default=0, help="0 = until Ctrl-C")
    s.set_defaults(func=cmd_stream)

    s = sub.add_parser("log", help="record to CSV with running statistics")
    s.add_argument("path")
    s.add_argument("-t", "--seconds", type=float, default=0)
    s.add_argument("-n", "--number", type=int, default=0)
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("calib", help="calibrate Cref against a known capacitor")
    s.add_argument("known", type=float, help="the fitted capacitance, in pF")
    s.add_argument("-c", "--channel", type=int, default=2)
    s.add_argument("-r", "--refsel", type=int, default=None)
    s.add_argument("-N", "--samples", type=int, default=16)
    s.add_argument("--sweep", action="store_true",
                   help="characterise every C_REF_SEL code (slow, ~2 min)")
    s.add_argument("-o", "--out", default=None)
    s.set_defaults(func=cmd_calib)

    s = sub.add_parser("speed", help="rate/resolution preset, then measure it")
    s.add_argument("preset", nargs="?", choices=sorted(SPEED_PRESETS))
    s.add_argument("-N", "--samples", type=int, default=24)
    s.set_defaults(func=cmd_speed)

    s = sub.add_parser("health", help="status flags, rail voltage, INTN")
    s.set_defaults(func=cmd_health)

    s = sub.add_parser("config", help="show or change front-end parameters")
    s.add_argument("--avrg", type=int, help="C_AVRG sample size, 1..%d" % C_AVRG_MAX)
    s.add_argument("--conv", type=int, help="CONV_TIME")
    s.add_argument("--refsel", type=int, help="C_REF_SEL 0..31")
    s.add_argument("--disch", type=int,
                   help="DISCHARGE_TIME; raise if a port reports C_PortError")
    s.add_argument("--comp", help="'i', 'e', 'ie' or '' - which compensations")
    s.add_argument("--ports", help="C_PORT_EN bitmask, e.g. 0x3F")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("console", help="send raw firmware commands")
    s.add_argument("command", nargs="+")
    s.set_defaults(func=cmd_console)

    a = p.parse_args(argv)
    try:
        a.func(a)
    except PCapError as e:
        sys.exit("error: %s" % e)
    except KeyboardInterrupt:
        sys.exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
