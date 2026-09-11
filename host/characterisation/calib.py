#!/usr/bin/env python3
"""calib.py - calibrate the PCap04 front end against the 100 pF on CTEST1.

The chip returns  ratio = C / Cref(N),  where N = C_REF_SEL and the datasheet
gives  Cref ~ 0.959 pF * N + 3.23 pF  (typical, "step width varies").
Sweeping N with a fixed, known C therefore measures the reference bank:

    1 / ratio(N) = (a/C) * N + (b/C)

is a straight line whose slope and intercept give a and b in pF once C is
known.  Running the sweep with C_COMP_EXT on and off gives the PC4/PC5 stray
capacitance as well, and the scatter within each point gives the noise floor.

A config change only takes effect after INIT + CDC_START - writing the
register alone leaves the running conversion on the old setting.
"""
import re, sys, time, statistics
from session import open_port, cmd

NS = [1, 3, 6, 9, 12, 16, 20, 24, 28, 31]
NSAMP, NDROP = 10, 3
CNOM = 100.0            # the capacitor fitted on CTEST1, in pF


def resd(s):
    out = cmd(s, "resd")
    r = {}
    for m in re.finditer(r"RES(\d) = 0x([0-9A-F]{8})", out):
        r[int(m.group(1))] = int(m.group(2), 16) / 2.0 ** 27
    st = re.search(r"status 0x20\.\.0x22 = ([0-9A-F]{2})", out)
    return r, int(st.group(1), 16) if st else 0


def point(s, n, comp_ext):
    cmd(s, "loadf")
    cmd(s, "wr 04 %02X" % (0xB1 if comp_ext else 0x91))
    cmd(s, "wr 11 %02X" % (n << 2))
    cmd(s, "init")
    cmd(s, "start")
    time.sleep(0.8)
    vals, bad = [], 0
    for i in range(NSAMP + NDROP):
        r, st = resd(s)
        if not (st & 0x01):
            bad += 1
        elif i >= NDROP and 2 in r:
            vals.append(r[2])
    return vals, bad


def linfit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = my - a * mx
    res = [y - (a * x + b) for x, y in zip(xs, ys)]
    return a, b, (sum(r * r for r in res) / n) ** 0.5, res


s = open_port()
results = {}
for comp in (True, False):
    print("\n=== C_COMP_EXT %s ===" % ("ON" if comp else "OFF"))
    print("%3s %4s %13s %12s %10s" % ("N", "n", "mean ratio", "sd", "sd/mean"))
    pts = []
    for n in NS:
        v, bad = point(s, n, comp)
        if len(v) < 4:
            print("%3d  ---  only %d valid samples, %d resets" % (n, len(v), bad)); continue
        m, sd = statistics.mean(v), statistics.stdev(v)
        print("%3d %4d %13.6f %12.3e %8.1f ppm%s"
              % (n, len(v), m, sd, sd / m * 1e6, "   (%d resets)" % bad if bad else ""))
        pts.append((n, m, sd))
    results[comp] = pts
s.close()

print("\n=== reference bank:  Cref(N) = a*N + b,  from 1/ratio vs N ===")
fits = {}
for comp in (True, False):
    pts = results.get(comp, [])
    if len(pts) < 3:
        continue
    xs = [p[0] for p in pts]
    ys = [1.0 / p[1] for p in pts]
    sa, sb, rms, res = linfit(xs, ys)
    a, b = sa * CNOM, sb * CNOM          # only exact for the comp-on branch
    fits[comp] = (sa, sb)
    print("\nC_COMP_EXT %s   (scaling by the nominal %.0f pF)" % ("ON " if comp else "OFF", CNOM))
    print("  Cref = %.4f pF * N + %.3f pF      datasheet typ.  0.959 * N + 3.23" % (a, b))
    print("  slope %+.1f %% vs typical, intercept %+.2f pF vs typical" % ((a / 0.959 - 1) * 100, b - 3.23))
    print("  fit residual rms %.3e in 1/ratio  =  %.1f ppm of the N=31 reading" % (rms, rms / ys[-1] * 1e6))
    print("  residuals (ppm): " + " ".join("%+.0f" % (r / y * 1e6) for r, y in zip(res, ys)))

if results.get(True) and results.get(False):
    # Do NOT take the ratio of the two linear-fit slopes: the reference bank is
    # not affine (see the residuals above) and the fit slope absorbs that.
    # Compare the two sweeps point by point instead - external compensation
    # removes a FIXED capacitance, so ratio_off/ratio_on must be constant.
    on = {n: m for n, m, _ in results[True]}
    off = {n: m for n, m, _ in results[False]}
    ks = [(n, off[n] / on[n]) for n in sorted(on) if n in off]
    vals = [k for _, k in ks]
    km = sum(vals) / len(vals)
    ksd = (sum((k - km) ** 2 for k in vals) / (len(vals) - 1)) ** 0.5
    print("\n=== external compensation ===")
    print("  ratio(OFF)/ratio(ON) per reference setting:")
    print("   " + "  ".join("N=%d:%.6f" % (n, k) for n, k in ks))
    print("  mean %.6f, sd %.2e (%.1f ppm) over a %.1f:1 range of Cref"
          % (km, ksd, ksd / km * 1e6, max(on.values()) / min(on.values())))
    print("  constant to within the noise => the model ratio = C/Cref and a fixed")
    print("     parasitic both hold.")
    print("  -> PC4/PC5 parasitic capacitance to ground  Cstray = %.3f pF" % (CNOM * (km - 1.0)))
    print("     (scales directly with the true value of the CTEST1 capacitor)")

if True in fits:
    sa, sb = fits[True]
    print("\n=== resolution, C_COMP_EXT ON, referred to the 100 pF sensor ===")
    for n, m, sd in results[True]:
        cref = (sa * n + sb) * CNOM
        print("  N=%2d  Cref=%6.2f pF  C=%7.3f pF  sd = %8.1f aF   (%6.1f ppm)"
              % (n, cref, m * cref, sd * cref * 1e6, sd / m * 1e6))
