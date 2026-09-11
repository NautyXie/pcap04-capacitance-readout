#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
pcap_gui.py - graphical front end for the PCAP04_Acquisition_3CH v1.5 board.

    python3 pcap_gui.py            # talks to the board over the console
    python3 pcap_gui.py --sim      # no hardware: a simulator with the same
                                   # behaviour, for trying out the layout

Pure tkinter - no matplotlib, no numpy.  The strip chart is drawn on a
Canvas, which is plenty for the chip's 3-13 Hz.

Threading: ONE worker thread owns the device (and therefore the serial
port).  The GUI never touches the device directly; it posts commands to the
worker and drains an event queue from a Tk timer.  While streaming, the
worker checks for commands between samples, closes the stream generator to
talk to the chip, and reopens it afterwards - so a mode switch or a speed
change mid-stream costs one round trip and nothing else.
"""

import argparse
import csv
import math
import os
import queue
import random
import statistics
import sys
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pcap04 import (Sample, Calibration, PCapError, SPEED_PRESETS,   # noqa: E402
                    FLOATING_CHANNELS, GROUNDED_CHANNELS, C_AVRG_MAX,
                    F_OLF_HZ, Q527)

APP_TITLE = "PCAP04 电容读出  ·  Acquisition_3CH v1.5"
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]
SPEED_LABELS = {"fast": "快速  C_AVRG=32  ~12.8 Hz",
                "balanced": "平衡  C_AVRG=128  ~12.8 Hz",
                "precise": "高分辨  C_AVRG=256  ~3.2 Hz"}
WINDOWS = [("10 s", 10), ("30 s", 30), ("1 min", 60), ("5 min", 300), ("30 min", 1800)]


# ============================================================================
# simulator - the same interface the GUI uses on PCap04
# ============================================================================

class SimDevice:
    """Behaves like the real board closely enough to exercise every control.

    Numbers come from the measured v1.5 board: Cref(N) ~ 0.975 N + 9.91 pF,
    a 100 pF capacitor on ch2 with 22.14 pF of trace stray, grounded
    electrodes at 12.8 / 13.9 / 17.4 / 13.8 / 132 / 135 pF, noise ~250 ppm at
    C_AVRG = 32 falling as 1/sqrt(C_AVRG), and the averaging-vs-period
    interaction that halves the rate when the conversion no longer fits.
    """

    port_name = "SIM"
    open_seconds = 0.0
    CAL_PATH = Calibration.DEFAULT_PATH.replace(".json", "_sim.json")

    def __init__(self):
        self.powered = False
        self.loaded = False
        self._mode = "f"
        self.refsel, self.avrg, self.conv = 31, 32, 2000
        self.comp_int, self.comp_ext, self.ports = True, True, 0x3F
        self.cal = Calibration.load(self.CAL_PATH) or Calibration.load()
        self._t0 = time.time()
        self._board_ms = random.randint(1000, 500000)
        self._last_raw = None

    # -- "physics" ---------------------------------------------------------
    def _cref(self, n=None):
        n = self.refsel if n is None else n
        wobble = 0.35 * math.sin(n * 1.7)                    # per-code DNL
        return 0.975 * n + 9.91 + wobble

    def _true_caps(self):
        t = time.time() - self._t0
        drift = 0.02 * math.sin(t / 40.0) + 0.004 * math.sin(t / 3.1)
        if self._mode == "f":
            return [None, None, 100.0 + drift]
        return [12.81, 13.94, 17.45, 13.78, 132.36 + drift, 134.82 + drift]

    def _noise_ppm(self):
        return 250.0 * math.sqrt(32.0 / max(1, self.avrg))

    def _period(self):
        trig = 2.0 * self.conv / F_OLF_HZ
        busy = 0.00057 * self.avrg
        k = max(1, int(math.ceil(busy / trig)))
        return k * trig

    def _sample(self):
        cref = self._cref()
        raws = [0] * 8
        for i, c in enumerate(self._true_caps()):
            if c is None:
                r = 15.99997 - random.random() * 2e-4 if self.avrg <= 64 else 13.81 + random.gauss(0, 2e-3)
            else:
                r = c / cref
                if self._mode == "f" and not self.comp_ext:
                    r += 22.14 / cref
                r *= 1.0 + random.gauss(0, self._noise_ppm() * 1e-6)
            raws[i] = max(0, min(0xFFFFFFFF, int(r * Q527)))
        raws[7] = int(0.223 * Q527)
        s0 = 0x09 | (0x02 if random.random() < 0.2 else 0)
        self._board_ms += int(self._period() * 1000)
        return Sample(self._board_ms, raws, (s0, 0x80, 0x00), self.cal, self._mode)

    # -- interface -----------------------------------------------------------
    def close(self):
        pass

    def cmd(self, line, **kw):
        time.sleep(0.05)
        return "  (sim) %s" % line

    def power(self, on=True):
        time.sleep(0.05)
        self.powered = bool(on)
        self.loaded = False
        return ("  +3V3P up in 4 ms, settled at 3.297 V\n  SPI buffer enabled" if on
                else "  SPI buffer tri-stated\n  +3V3P disabled")

    def power_cycle(self, off_ms=200):
        self.power(False); time.sleep(off_ms / 1000.0); return self.power(True)

    def rail_mv(self):
        return 3301.0 + random.gauss(0, 2) if self.powered else 12.0

    def load(self, mode="f"):
        if not self.powered:
            raise PCapError("ERR +3V3P is off - run 'rail on' first")
        time.sleep(1.2)
        self._mode = mode[:1]
        self.loaded = True
        self.refsel, self.avrg, self.conv = 31, 32, 2000     # load resets config
        self.comp_int, self.comp_ext, self.ports = True, self._mode == "f", 0x3F
        return ("  test read ... 0x11 OK\n  writing 548 bytes of standard firmware to SRAM ... verified\n"
                "  writing 52 config registers (%s) ... verified\n  CDC_START (0x8C) - conversions are running"
                % ("3x floating, internal reference" if self._mode == "f" else "6x grounded, internal reference"))

    def mode(self, m=None):
        if m is None:
            return self._mode
        if not self.loaded:
            return self.load(m)
        time.sleep(0.08)
        self._mode = m[:1]
        self.comp_ext = self._mode == "f"
        return "  mode = %s" % ("FLOATING (3 pairs -> RES0..RES2)" if self._mode == "f"
                                 else "GROUNDED (6 electrodes -> RES0..RES5)")

    def _ensure_loaded(self):
        if not self.loaded:
            self.load(self._mode)

    def set_avrg(self, n):
        self._ensure_loaded(); time.sleep(0.08)
        n = max(1, min(C_AVRG_MAX, int(n))); self.avrg = n
        return "  C_AVRG = %d" % n

    def set_conv(self, n):
        self._ensure_loaded(); time.sleep(0.08)
        self.conv = max(25, int(n))
        return "  CONV_TIME = %d  -> nominal %.3f Hz" % (self.conv, F_OLF_HZ / (2 * self.conv))

    def set_refsel(self, n):
        self._ensure_loaded(); time.sleep(0.08)
        self.refsel = max(0, min(31, int(n)))
        if self.cal:
            self.cal.refsel = self.refsel
        return "  C_REF_SEL = %d" % self.refsel

    def set_comp(self, internal=True, external=True):
        self._ensure_loaded(); time.sleep(0.08)
        self.comp_int = bool(internal)
        self.comp_ext = bool(external) and self._mode == "f"
        return "  C_COMP_INT=%d C_COMP_EXT=%d" % (self.comp_int, self.comp_ext)

    def set_ports(self, mask):
        self._ensure_loaded(); time.sleep(0.08)
        self.ports = mask & 0x3F
        return "  C_PORT_EN = 0x%02X" % self.ports

    def speed(self, preset):
        a, c = SPEED_PRESETS[preset]
        self.set_avrg(a); return self.set_conv(c)

    def params(self):
        time.sleep(0.05)
        raw = ("  firmware in PCAP04 SRAM : %s\n  mode                    : %s\n"
               "  C_REF_INT               : 1   C_REF_SEL = %d\n"
               "  C_COMP_INT / C_COMP_EXT : %d / %d\n  C_PORT_EN               : 0x%02X\n"
               "  C_AVRG                  : %d\n  CONV_TIME               : %d  -> nominal %.3f Hz\n"
               "  WD_DIS                  : 0x5A (watchdog off)\n  RUNBIT (cfg 0x2F)       : 0x%02X"
               % ("loaded" if self.loaded else "NOT loaded - run 'load'",
                  "FLOATING" if self._mode == "f" else "GROUNDED", self.refsel,
                  self.comp_int, self.comp_ext, self.ports, self.avrg, self.conv,
                  F_OLF_HZ / (2 * self.conv), 1 if self.loaded else 0))
        return {"raw": raw, "loaded": self.loaded, "mode": "FLOATING" if self._mode == "f" else "GROUNDED",
                "refsel": self.refsel, "avrg": self.avrg, "conv_time": self.conv,
                "port_en": self.ports, "comp_int": self.comp_int, "comp_ext": self.comp_ext,
                "watchdog_off": True}

    def health(self):
        time.sleep(0.05)
        if not self.powered:
            raw = ("  STATUS_0 = 0xFF :  (no reply - rail is off)\n  +3V3P rail      = OFF, measured 0.012 V")
            return {"raw": raw, "runbit": False, "errors": [], "port_errors": []}
        rb = self.loaded
        raw = ("  STATUS_0 = 0x%02X :%s\n  STATUS_1 = 0x80 : no errors\n  STATUS_2 = 0x00 : no port errors\n"
               "  INTN pin (PB1)  = %d  (active low)\n  +3V3P rail      = ON, measured %.3f V"
               % (0x09 if rb else 0x00, " CDC_ACTIVE RUNBIT" if rb else " *** RUNBIT CLEAR - chip is idle ***",
                  1 if rb else 0, self.rail_mv() / 1000))
        return {"raw": raw, "runbit": rb, "errors": [], "port_errors": []}

    def read(self, fresh=True, tries=40):
        self._ensure_loaded(); time.sleep(self._period())
        return self._sample()

    def stream(self, n=0, timeout=10.0):
        if not self.powered:
            raise PCapError("ERR +3V3P is off - run 'rail on' first")
        self._ensure_loaded()
        k = 0
        while n == 0 or k < n:
            time.sleep(self._period())
            yield self._sample()
            k += 1

    def collect(self, n=30, timeout=25.0, settle=2):
        got = list(self.stream(n + settle))
        return got[settle:]

    @staticmethod
    def stats(samples, channel=0):
        from pcap04 import PCap04
        return PCap04.stats(samples, channel)

    def calibrate(self, known_pf, channel=2, refsel=None, n=16, sweep=False, measure_stray=True):
        self._ensure_loaded()
        cal = self.cal or Calibration()
        codes = list(range(1, 32)) if sweep else [refsel if refsel is not None else self.refsel]
        for code in codes:
            self.set_refsel(code)
            r = statistics.mean(x.channels[channel] for x in self.collect(min(n, 6)))
            if r > 0:
                cal.cref[int(code)] = known_pf / r
        if measure_stray and self._mode == "f":
            self.set_comp(True, True); on = statistics.mean(x.channels[channel] for x in self.collect(6))
            self.set_comp(True, False); off = statistics.mean(x.channels[channel] for x in self.collect(6))
            self.set_comp(True, True)
            cal.stray[int(channel)] = known_pf * (off / on - 1.0)
        cal.refsel = int(codes[-1]); self.refsel = cal.refsel
        cal.note = "SIM: channel %d against %.4f pF" % (channel, known_pf)
        self.cal = cal
        return cal


# ============================================================================
# worker thread
# ============================================================================

class Worker(threading.Thread):
    def __init__(self, dev, events):
        super().__init__(daemon=True)
        self.dev, self.events = dev, events
        self.cmds = queue.Queue()
        self.streaming = False
        self._announced = False
        self._quit = threading.Event()

    def call(self, name, fn, *a, **kw):
        self.cmds.put((name, fn, a, kw))

    def stop(self):
        self._quit.set()

    def run(self):
        gen = None
        while not self._quit.is_set():
            try:
                name, fn, a, kw = self.cmds.get(timeout=0.0 if (gen or self.streaming) else 0.1)
            except queue.Empty:
                name = None
            if name is not None:
                if gen is not None:
                    try:
                        gen.close()
                    except Exception:
                        pass
                    gen = None
                try:
                    self.events.put(("result", name, fn(*a, **kw)))
                except Exception as e:                      # noqa: BLE001
                    self.events.put(("error", name, str(e)))
                continue
            if self.streaming:
                if gen is None:
                    try:
                        gen = self.dev.stream(0)
                        if not self._announced:
                            self._announced = True
                            self.events.put(("started",))
                    except Exception as e:                  # noqa: BLE001
                        self.events.put(("error", "stream", str(e)))
                        self.streaming = False
                        continue
                try:
                    self.events.put(("sample", next(gen)))
                except StopIteration:
                    gen = None
                except Exception as e:                      # noqa: BLE001
                    self.events.put(("error", "stream", str(e)))
                    gen = None
                    self.streaming = False
            elif gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass
                gen = None
                self._announced = False
                self.events.put(("log", "采集停止"))
        if gen is not None:
            try:
                gen.close()
            except Exception:
                pass
        try:
            self.dev.close()
        except Exception:
            pass


# ============================================================================
# strip chart on a Canvas
# ============================================================================

class StripChart(tk.Canvas):
    PAD_L, PAD_R, PAD_T, PAD_B = 74, 16, 24, 30

    def __init__(self, master, **kw):
        super().__init__(master, bg="white", highlightthickness=0, **kw)
        self.data = [deque(maxlen=20000) for _ in range(6)]
        self.visible = [True] * 6
        self.window_s = 30
        self.ymode = "auto"              # auto | ac | manual
        self.ymin, self.ymax = 0.0, 1.0
        self.unit = ""
        self.nch = 3
        self.bind("<Configure>", lambda e: self.redraw())

    def clear(self):
        for d in self.data:
            d.clear()
        self.redraw()

    def push(self, t, values):
        for i, v in enumerate(values):
            if v is not None:
                self.data[i].append((t, v))

    @staticmethod
    def _nice(span):
        if span <= 0:
            return 1.0
        p = 10 ** math.floor(math.log10(span))
        for m in (1, 2, 2.5, 5, 10):
            if span / (m * p) <= 6:
                return m * p
        return 10 * p

    def redraw(self):
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 50 or H < 50:
            return
        x0, x1 = self.PAD_L, W - self.PAD_R
        y0, y1 = self.PAD_T, H - self.PAD_B
        now = time.time()
        tmin = now - self.window_s

        series = []
        for i in range(self.nch):
            if not self.visible[i]:
                continue
            pts = [(t, v) for t, v in self.data[i] if t >= tmin]
            if not pts:
                continue
            if self.ymode == "ac":
                m = sum(v for _, v in pts) / len(pts)
                pts = [(t, v - m) for t, v in pts]
            series.append((i, pts))

        if self.ymode == "manual":
            lo, hi = self.ymin, self.ymax
        else:
            vals = [v for _, pts in series for _, v in pts]
            if vals:
                lo, hi = min(vals), max(vals)
            else:
                lo, hi = 0.0, 1.0
            if hi - lo < 1e-12:
                lo, hi = lo - 1.0, hi + 1.0
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
        if hi <= lo:
            hi = lo + 1.0

        def X(t):
            return x0 + (t - tmin) / self.window_s * (x1 - x0)

        def Y(v):
            return y1 - (v - lo) / (hi - lo) * (y1 - y0)

        # frame + grid
        self.create_rectangle(x0, y0, x1, y1, outline="#cfd8dc")
        step = self._nice(hi - lo)
        v = math.ceil(lo / step) * step
        while v <= hi + 1e-12:
            y = Y(v)
            self.create_line(x0, y, x1, y, fill="#eceff1")
            label = ("%+.4g" if self.ymode == "ac" else "%.6g") % v
            self.create_text(x0 - 6, y, text=label, anchor="e", font=("TkDefaultFont", 9), fill="#455a64")
            v += step
        tstep = self._nice(self.window_s)
        k = math.ceil(tmin / tstep) * tstep
        while k <= now:
            x = X(k)
            self.create_line(x, y0, x, y1, fill="#eceff1")
            self.create_text(x, y1 + 6, text="%.0f s" % (k - now), anchor="n",
                             font=("TkDefaultFont", 9), fill="#455a64")
            k += tstep
        ylabel = {"ac": "Δ " + self.unit, "auto": self.unit, "manual": self.unit}[self.ymode]
        self.create_text(x0, y0 - 6, text=ylabel, anchor="sw", font=("TkDefaultFont", 9, "bold"),
                         fill="#37474f")

        # traces
        for i, pts in series:
            coords = []
            for t, val in pts:
                coords += [X(t), min(max(Y(val), y0 - 2), y1 + 2)]
            if len(coords) >= 4:
                self.create_line(*coords, fill=COLORS[i], width=1.6)
            elif len(coords) == 2:
                self.create_oval(coords[0] - 2, coords[1] - 2, coords[0] + 2, coords[1] + 2,
                                 fill=COLORS[i], outline="")
        if not series:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="无数据 —— 按「开始采集」",
                             fill="#90a4ae", font=("TkDefaultFont", 12))


# ============================================================================
# per-channel readout card
# ============================================================================

class ChannelCard(ttk.Frame):
    def __init__(self, master, idx, on_toggle, compact=False):
        super().__init__(master, padding=(6, 4), relief="groove", borderwidth=1)
        self.idx = idx
        self.var_show = tk.BooleanVar(value=True)
        big = 17 if compact else 20
        self.compact = compact
        top = ttk.Frame(self); top.pack(fill="x")
        sw = tk.Canvas(top, width=12, height=12, highlightthickness=0)
        sw.create_rectangle(1, 1, 11, 11, fill=COLORS[idx], outline="")
        sw.pack(side="left", padx=(0, 5))
        self.lbl_name = ttk.Label(top, text="ch%d" % idx, font=("TkDefaultFont", 11, "bold"))
        self.lbl_name.pack(side="left")
        ttk.Checkbutton(top, text="显示", variable=self.var_show,
                        command=lambda: on_toggle(idx, self.var_show.get())).pack(side="right")
        self.lbl_where = ttk.Label(self, text="", foreground="#607d8b", font=("TkDefaultFont", 9)); self.lbl_where.pack(anchor="w")
        self.lbl_val = ttk.Label(self, text="——", font=("TkFixedFont", big, "bold")); self.lbl_val.pack(anchor="w", pady=(1, 0))
        self.lbl_unit = ttk.Label(self, text="", foreground="#607d8b", font=("TkDefaultFont", 9)); self.lbl_unit.pack(anchor="w")
        self.lbl_stat = ttk.Label(self, text="", font=("TkFixedFont", 9), justify="left"); self.lbl_stat.pack(anchor="w", pady=(3, 0))
        self.hist = deque(maxlen=400)

    def set_identity(self, mode):
        table = FLOATING_CHANNELS if mode == "f" else GROUNDED_CHANNELS
        port, conn = table.get(self.idx, ("?", "?"))
        self.lbl_name.config(text="ch%d" % self.idx)
        self.lbl_where.config(text="%s  ·  %s" % (port, conn))
        self.hist.clear()
        self.lbl_val.config(text="——", foreground="black"); self.lbl_unit.config(text=""); self.lbl_stat.config(text="")

    def update(self, ratio, pf, is_open):
        if is_open:
            self.lbl_val.config(text="open", foreground="#b0bec5")
            self.lbl_unit.config(text="满量程，未接传感器")
            self.lbl_stat.config(text="")
            return
        v = pf if pf is not None else ratio
        self.hist.append(v)
        self.lbl_val.config(text=("%.4f" if pf is not None else "%.6f") % v, foreground="black")
        self.lbl_unit.config(text=("pF   比值 %.6f" % ratio) if pf is not None else "比值 C/Cref（未标定）")
        if len(self.hist) >= 3:
            m, sd = statistics.mean(self.hist), statistics.stdev(self.hist)
            ppm = sd / m * 1e6 if m else 0
            noise = ("σ %.1f fF" % (sd * 1000)) if pf is not None else ("σ %.2e" % sd)
            if self.compact:
                self.lbl_stat.config(text="%s  %.0f ppm\n%.4f … %.4f  n=%d"
                                     % (noise, ppm, min(self.hist), max(self.hist), len(self.hist)))
            else:
                self.lbl_stat.config(text="%s  %.0f ppm\nmin %.4f   max %.4f   n=%d"
                                     % (noise, ppm, min(self.hist), max(self.hist), len(self.hist)))


# ============================================================================
# main window
# ============================================================================

class App(tk.Tk):
    def __init__(self, sim=False, port=None):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x800")
        self.minsize(980, 640)
        self.sim, self.port = sim, port
        self.dev = None
        self.worker = None
        self.events = queue.Queue()
        self.mode = "f"
        self.nch = 3
        self.samples = 0
        self.rate_hist = deque(maxlen=40)
        self.rec_file = None
        self.rec_writer = None
        self.rec_count = 0
        self.busy = False
        self.last_sample_t = None
        self._build()
        self.after(60, self._pump)
        self.after(120, self._tick_chart)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if sim:
            self.after(200, self._connect)

    # -- layout ----------------------------------------------------------------
    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam" if sys.platform != "darwin" else "aqua")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("TkDefaultFont", 11, "bold"), padding=(10, 6))
        style.configure("Rec.TButton", foreground="#c62828")

        # ---- toolbar row 1: connection / power / mode / speed / run
        tb = ttk.Frame(self, padding=(8, 6)); tb.pack(fill="x")

        g = ttk.LabelFrame(tb, text="连接", padding=(6, 2)); g.pack(side="left", padx=(0, 8))
        self.var_port = tk.StringVar(value=self.port or "")
        self.cb_port = ttk.Combobox(g, textvariable=self.var_port, width=22, values=self._scan_ports())
        self.cb_port.pack(side="left")
        self.var_sim = tk.BooleanVar(value=self.sim)
        ttk.Checkbutton(g, text="模拟", variable=self.var_sim).pack(side="left", padx=4)
        self.btn_conn = ttk.Button(g, text="连接", command=self._toggle_connect); self.btn_conn.pack(side="left")

        g = ttk.LabelFrame(tb, text="电源 U5 (+3V3P)", padding=(6, 2)); g.pack(side="left", padx=(0, 8))
        self.btn_pon = ttk.Button(g, text="开", width=4, command=lambda: self._call("power on", lambda: self.dev.power(True)))
        self.btn_poff = ttk.Button(g, text="关", width=4, command=lambda: self._call("power off", lambda: self.dev.power(False)))
        self.btn_pcyc = ttk.Button(g, text="上下电", width=6, command=lambda: self._call("power cycle", lambda: self.dev.power_cycle()))
        for b in (self.btn_pon, self.btn_poff, self.btn_pcyc):
            b.pack(side="left", padx=1)
        self.lbl_rail = ttk.Label(g, text="—— V", width=8, anchor="center"); self.lbl_rail.pack(side="left", padx=(6, 0))

        g = ttk.LabelFrame(tb, text="模式", padding=(6, 2)); g.pack(side="left", padx=(0, 8))
        self.var_mode = tk.StringVar(value="f")
        ttk.Radiobutton(g, text="悬浮 3 对", variable=self.var_mode, value="f", command=self._on_mode).pack(side="left")
        ttk.Radiobutton(g, text="接地 6 电极", variable=self.var_mode, value="g", command=self._on_mode).pack(side="left", padx=(6, 0))
        self.btn_reload = ttk.Button(g, text="重载固件", command=self._on_reload); self.btn_reload.pack(side="left", padx=(8, 0))

        g = ttk.LabelFrame(tb, text="档位", padding=(6, 2)); g.pack(side="left", padx=(0, 8))
        self.var_speed = tk.StringVar(value=SPEED_LABELS["fast"])
        cb = ttk.Combobox(g, textvariable=self.var_speed, state="readonly", width=26,
                          values=[SPEED_LABELS[k] for k in ("fast", "balanced", "precise")])
        cb.pack(side="left"); cb.bind("<<ComboboxSelected>>", self._on_speed)

        # ---- toolbar row 2: run / record / tools
        tbr = ttk.Frame(self, padding=(8, 0, 8, 4)); tbr.pack(fill="x")
        g = ttk.LabelFrame(tbr, text="采集", padding=(6, 2)); g.pack(side="left", padx=(0, 8))
        self.btn_run = ttk.Button(g, text="▶ 开始采集", style="Big.TButton", command=self._toggle_run); self.btn_run.pack(side="left")
        self.btn_rec = ttk.Button(g, text="● 录制 CSV", command=self._toggle_rec); self.btn_rec.pack(side="left", padx=(6, 0))
        self.lbl_rec = ttk.Label(g, text="", width=28); self.lbl_rec.pack(side="left", padx=(6, 0))

        g = ttk.Frame(tbr); g.pack(side="right")
        ttk.Button(g, text="设置…", command=self._dlg_settings).pack(side="left", padx=2)
        ttk.Button(g, text="标定…", command=self._dlg_calib).pack(side="left", padx=2)
        ttk.Button(g, text="健康检查", command=lambda: self._call("health", lambda: self.dev.health())).pack(side="left", padx=2)

        # ---- toolbar row 3: display
        tb2 = ttk.Frame(self, padding=(8, 0, 8, 6)); tb2.pack(fill="x")
        ttk.Label(tb2, text="时间窗").pack(side="left")
        self.var_win = tk.StringVar(value="30 s")
        cb = ttk.Combobox(tb2, textvariable=self.var_win, state="readonly", width=7, values=[w[0] for w in WINDOWS])
        cb.pack(side="left", padx=(4, 12)); cb.bind("<<ComboboxSelected>>", self._on_window)
        ttk.Label(tb2, text="纵轴").pack(side="left")
        self.var_y = tk.StringVar(value="auto")
        for txt, val in (("自动", "auto"), ("去均值 (看变化)", "ac"), ("手动", "manual")):
            ttk.Radiobutton(tb2, text=txt, variable=self.var_y, value=val, command=self._on_ymode).pack(side="left", padx=(6, 0))
        self.var_ymin, self.var_ymax = tk.StringVar(value="0"), tk.StringVar(value="200")
        ttk.Label(tb2, text=" 从").pack(side="left")
        ttk.Entry(tb2, textvariable=self.var_ymin, width=8).pack(side="left")
        ttk.Label(tb2, text="到").pack(side="left")
        ttk.Entry(tb2, textvariable=self.var_ymax, width=8).pack(side="left")
        ttk.Button(tb2, text="应用", command=self._on_ymode).pack(side="left", padx=(4, 12))
        ttk.Button(tb2, text="清除曲线", command=lambda: self.chart.clear()).pack(side="left")
        self.lbl_calinfo = ttk.Label(tb2, text="", foreground="#607d8b"); self.lbl_calinfo.pack(side="right")

        # ---- status bar (packed first at the bottom so it always stays visible)
        sbf = ttk.Frame(self, padding=(8, 3), relief="sunken"); sbf.pack(fill="x", side="bottom")
        self.st_state = ttk.Label(sbf, text="未连接", width=22); self.st_state.pack(side="left")
        self.st_rate = ttk.Label(sbf, text="速率 —", width=14); self.st_rate.pack(side="left")
        self.st_n = ttk.Label(sbf, text="样本 0", width=12); self.st_n.pack(side="left")
        self.st_cfg = ttk.Label(sbf, text=""); self.st_cfg.pack(side="left", padx=(8, 0))
        self.st_err = ttk.Label(sbf, text="", foreground="#c62828"); self.st_err.pack(side="right")

        # ---- log
        logf = ttk.LabelFrame(self, text="日志", padding=(4, 2)); logf.pack(fill="x", side="bottom", padx=8, pady=(4, 2))
        self.log = tk.Text(logf, height=5, font=("TkFixedFont", 10), state="disabled", wrap="none", relief="flat")
        sb = ttk.Scrollbar(logf, command=self.log.yview); self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")

        # ---- body: chart + cards
        body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=8)
        right = ttk.Frame(body, width=320); right.pack(side="right", fill="y", padx=(8, 0))
        right.pack_propagate(False)
        self.cards_frame = right
        self.cards = []
        self.chart = StripChart(body); self.chart.pack(side="left", fill="both", expand=True)
        self._rebuild_cards()
        self._set_enabled(False)

    def _rebuild_cards(self):
        for c in self.cards:
            c.destroy()
        self.cards = []
        cols = 1 if self.nch <= 3 else 2
        for col in range(2):
            self.cards_frame.columnconfigure(col, weight=(1 if col < cols else 0),
                                             uniform=("cards" if cols == 2 else ""))
        for i in range(self.nch):
            c = ChannelCard(self.cards_frame, i, self._on_card_toggle, compact=(cols == 2))
            c.grid(row=i // cols, column=i % cols, sticky="nsew", padx=2, pady=2)
            c.set_identity(self.mode)
            self.cards.append(c)
        self.chart.nch = self.nch
        self.chart.visible = [True] * 6
        self.chart.clear()

    def _set_enabled(self, on):
        st = "normal" if on else "disabled"
        for w in (self.btn_pon, self.btn_poff, self.btn_pcyc, self.btn_reload, self.btn_run, self.btn_rec):
            w.config(state=st)

    # -- helpers --------------------------------------------------------------------
    @staticmethod
    def _scan_ports():
        import glob
        return sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*") + glob.glob("COM[0-9]*"))

    def _logline(self, txt, err=False):
        self.log.config(state="normal")
        stamp = time.strftime("%H:%M:%S")
        for ln in str(txt).rstrip().splitlines() or [""]:
            self.log.insert("end", "%s  %s\n" % (stamp, ln), ("err",) if err else ())
        self.log.tag_config("err", foreground="#c62828")
        self.log.see("end")
        self.log.config(state="disabled")

    def _call(self, name, fn):
        if not self.worker:
            return
        self.busy = True
        self.st_state.config(text="执行: %s …" % name)
        self.worker.call(name, fn)

    def _units(self):
        cal = self.dev.cal if self.dev else None
        return ("pF", True) if (cal and cal.cref_pf()) else ("比值", False)

    # -- connection -------------------------------------------------------------------
    def _toggle_connect(self):
        if self.worker:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        try:
            if self.var_sim.get():
                self.dev = SimDevice()
            else:
                from pcap04 import PCap04
                self.dev = PCap04(port=self.var_port.get() or None)
            self.worker = Worker(self.dev, self.events)
            self.worker.start()
        except Exception as e:                                  # noqa: BLE001
            self.dev = None
            messagebox.showerror("连接失败", str(e))
            return
        self.btn_conn.config(text="断开")
        self._set_enabled(True)
        self.st_state.config(text="已连接 %s" % self.dev.port_name)
        self._logline("已连接 %s%s" % (self.dev.port_name, "  （模拟数据）" if self.var_sim.get() else ""))
        self._refresh_calinfo()
        self._call("params", lambda: self.dev.params())
        self._call("rail", lambda: self.dev.rail_mv())

    def _disconnect(self):
        self._stop_rec()
        if self.worker:
            self.worker.streaming = False
            self.worker.stop()
            self.worker = None
        self.dev = None
        self.btn_conn.config(text="连接")
        self.btn_run.config(text="▶ 开始采集")
        self._set_enabled(False)
        self.st_state.config(text="未连接")
        self._logline("已断开")

    def _on_close(self):
        self._disconnect()
        self.destroy()

    # -- actions --------------------------------------------------------------------
    def _toggle_run(self):
        if not self.worker:
            return
        if self.worker.streaming:
            self.worker.streaming = False
            self.btn_run.config(text="▶ 开始采集")
        else:
            self.rate_hist.clear()
            self.last_sample_t = None
            self.worker.streaming = True
            self.btn_run.config(text="■ 停止采集")

    def _on_mode(self):
        m = self.var_mode.get()
        if not self.worker:
            self.mode, self.nch = m, (3 if m == "f" else 6)
            self._rebuild_cards()
            return
        self._call("mode " + m, lambda: self.dev.mode(m))

    def _on_reload(self):
        m = self.var_mode.get()
        self._call("load " + m, lambda: self.dev.load(m))

    def _on_speed(self, *_):
        key = [k for k, v in SPEED_LABELS.items() if v == self.var_speed.get()][0]
        self._call("speed " + key, lambda: self.dev.speed(key))

    def _on_window(self, *_):
        self.chart.window_s = dict(WINDOWS)[self.var_win.get()]
        self.chart.redraw()

    def _on_ymode(self):
        self.chart.ymode = self.var_y.get()
        if self.chart.ymode == "manual":
            try:
                self.chart.ymin, self.chart.ymax = float(self.var_ymin.get()), float(self.var_ymax.get())
            except ValueError:
                messagebox.showwarning("纵轴", "手动范围请填数字")
        unit, calib = self._units()
        self.chart.unit = (unit + (" (fF)" if calib and self.chart.ymode == "ac" else "")) if unit else ""
        self.chart.redraw()

    def _on_card_toggle(self, idx, show):
        self.chart.visible[idx] = show
        self.chart.redraw()

    # -- recording -----------------------------------------------------------------------
    def _toggle_rec(self):
        if self.rec_file:
            self._stop_rec()
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                            initialfile=time.strftime("pcap04_%Y%m%d_%H%M%S.csv"))
        if not path:
            return
        self.rec_file = open(path, "w", newline="")
        self.rec_writer = csv.writer(self.rec_file)
        n = self.nch
        self.rec_writer.writerow(["host_time", "board_t_ms"] + ["ch%d_raw" % i for i in range(n)]
                                 + ["ch%d_ratio" % i for i in range(n)] + ["ch%d_pF" % i for i in range(n)]
                                 + ["status0", "status1", "status2", "errors"])
        self.rec_count = 0
        self.btn_rec.config(text="■ 停止录制", style="Rec.TButton")
        self.lbl_rec.config(text=os.path.basename(path))
        self._logline("录制到 " + path)

    def _stop_rec(self):
        if self.rec_file:
            self.rec_file.close()
            self._logline("录制结束，%d 行" % self.rec_count)
        self.rec_file = self.rec_writer = None
        self.btn_rec.config(text="● 录制 CSV", style="TButton")
        self.lbl_rec.config(text="")

    # -- dialogs -------------------------------------------------------------------------
    def _dlg_settings(self):
        if not self.worker:
            messagebox.showinfo("设置", "先连接"); return
        d = tk.Toplevel(self); d.title("前端参数"); d.transient(self); d.grab_set()
        f = ttk.Frame(d, padding=12); f.pack()
        p = self.dev.params() if isinstance(self.dev, SimDevice) else {}
        rows = [("参考电容档 C_REF_SEL (0–31)", "refsel", p.get("refsel", 31)),
                ("平均次数 C_AVRG (1–%d)" % C_AVRG_MAX, "avrg", p.get("avrg", 32)),
                ("转换周期 CONV_TIME (周期 = 2·n / 51 kHz)", "conv", p.get("conv_time", 2000))]
        vars_ = {}
        for r, (lab, key, val) in enumerate(rows):
            ttk.Label(f, text=lab).grid(row=r, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(val)); vars_[key] = v
            ttk.Entry(f, textvariable=v, width=10).grid(row=r, column=1, padx=8)
        vi, ve = tk.BooleanVar(value=p.get("comp_int", True)), tk.BooleanVar(value=p.get("comp_ext", True))
        ttk.Checkbutton(f, text="片内补偿 C_COMP_INT", variable=vi).grid(row=3, column=0, sticky="w", pady=3)
        ttk.Checkbutton(f, text="片外补偿 C_COMP_EXT（仅悬浮）", variable=ve).grid(row=4, column=0, sticky="w")
        ttk.Label(f, text="端口使能 C_PORT_EN (hex)").grid(row=5, column=0, sticky="w", pady=3)
        vp = tk.StringVar(value="0x%02X" % p.get("port_en", 0x3F)); ttk.Entry(f, textvariable=vp, width=10).grid(row=5, column=1)
        ttk.Label(f, foreground="#607d8b", justify="left", wraplength=380,
                  text="注意：C_AVRG ≥ 512 在本板会让读数偏低约 19.5 %，固件会拦在 256。"
                       "改 C_REF_SEL 之后 pF 标定只对已标定过的档有效。").grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 4))

        def apply():
            try:
                rs, av, cv = int(vars_["refsel"].get()), int(vars_["avrg"].get()), int(vars_["conv"].get())
                pm = int(vp.get(), 0)
            except ValueError:
                messagebox.showwarning("设置", "请填整数"); return
            dev = self.dev
            def do():
                out = [dev.set_refsel(rs), dev.set_avrg(av), dev.set_conv(cv),
                       dev.set_comp(vi.get(), ve.get()), dev.set_ports(pm)]
                return "\n".join(out)
            self._call("settings", do)
            d.destroy()
        bb = ttk.Frame(f); bb.grid(row=7, column=0, columnspan=2, sticky="e")
        ttk.Button(bb, text="取消", command=d.destroy).pack(side="right")
        ttk.Button(bb, text="应用", command=apply).pack(side="right", padx=6)

    def _dlg_calib(self):
        if not self.worker:
            messagebox.showinfo("标定", "先连接"); return
        d = tk.Toplevel(self); d.title("标定 Cref"); d.transient(self); d.grab_set()
        f = ttk.Frame(d, padding=12); f.pack()
        ttk.Label(f, justify="left", wraplength=420,
                  text="在某一通道接一个已知电容，程序用它反推片内参考电容 Cref，之后所有通道直接显示 pF。\n"
                       "不要用手册公式：这颗芯片实测 Cref ≈ 0.975·N + 9.91 pF，比手册多约 7 pF。").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(f, text="已知电容 (pF)").grid(row=1, column=0, sticky="w", pady=3)
        vk = tk.StringVar(value="100"); ttk.Entry(f, textvariable=vk, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="接在通道").grid(row=2, column=0, sticky="w", pady=3)
        vc = tk.StringVar(value="2"); ttk.Combobox(f, textvariable=vc, width=7, state="readonly",
                                                    values=[str(i) for i in range(self.nch)]).grid(row=2, column=1, sticky="w")
        vs = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="扫描全部 31 档参考电容（约 2 分钟，做一次就够）", variable=vs).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        cal = self.dev.cal
        cur = ("当前：Cref(N=%d) = %.3f pF" % (cal.refsel, cal.cref_pf()) if (cal and cal.cref_pf()) else "当前：未标定")
        ttk.Label(f, text=cur, foreground="#607d8b").grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 4))

        def go():
            try:
                known = float(vk.get()); ch = int(vc.get())
            except ValueError:
                messagebox.showwarning("标定", "请填数字"); return
            was = self.worker.streaming
            self.worker.streaming = False
            self.btn_run.config(text="▶ 开始采集")
            dev, sweep = self.dev, vs.get()
            def do():
                c = dev.calibrate(known, channel=ch, sweep=sweep)
                # the simulator keeps its own file so it can never clobber
                # the calibration measured on the real board
                path = c.save(dev.CAL_PATH if isinstance(dev, SimDevice) else None)
                return c, path
            self._call("calibrate", do)
            self._logline("标定开始：ch%d = %.4f pF%s（采集已暂停）" % (ch, known, "，扫描全部档位" if sweep else ""))
            d.destroy()
        bb = ttk.Frame(f); bb.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(bb, text="取消", command=d.destroy).pack(side="right")
        ttk.Button(bb, text="开始标定", command=go).pack(side="right", padx=6)

    def _refresh_calinfo(self):
        cal = self.dev.cal if self.dev else None
        if cal and cal.cref_pf():
            st = ("  寄生 " + ", ".join("ch%d %.2f pF" % (k, v) for k, v in sorted(cal.stray.items()))) if cal.stray else ""
            self.lbl_calinfo.config(text="标定：Cref(N=%d) = %.3f pF%s" % (cal.refsel, cal.cref_pf(), st))
        else:
            self.lbl_calinfo.config(text="未标定 —— 显示比值；用「标定…」得到 pF")
        self._on_ymode()

    # -- event pump -----------------------------------------------------------------
    def _pump(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "sample":
                    self._on_sample(ev[1])
                elif kind == "result":
                    self._on_result(ev[1], ev[2])
                elif kind == "error":
                    self.busy = False
                    self.st_err.config(text=ev[2].splitlines()[0][:90])
                    self._logline("%s 失败: %s" % (ev[1], ev[2]), err=True)
                    self.st_state.config(text="已连接 %s" % (self.dev.port_name if self.dev else ""))
                    if ev[1] == "stream":
                        self.btn_run.config(text="▶ 开始采集")
                elif kind == "started":
                    self._logline("采集开始")
                    # the first stream after a power cycle uploads the firmware,
                    # which resets the config - show what the chip really has
                    if self.worker:
                        self.worker.call("params", self.dev.params)
                elif kind == "log":
                    self._logline(ev[1])
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def _on_result(self, name, res):
        self.busy = False
        self.st_state.config(text="已连接 %s" % self.dev.port_name)
        if name == "rail":
            if res is not None:
                self.lbl_rail.config(text="%.3f V" % (res / 1000.0),
                                     foreground=("#2e7d32" if res > 3000 else "#c62828"))
            return
        if name == "params":
            self._apply_params(res); return
        if name == "health":
            self._logline(res["raw"]); return
        if name == "calibrate":
            cal, path = res
            self.dev.cal = cal
            self._logline("标定完成：Cref(N=%d) = %.4f pF；已存 %s" % (cal.refsel, cal.cref_pf(), path))
            for ch, st in cal.stray.items():
                self._logline("  ch%d 走线对地寄生 = %.3f pF（片外补偿已扣除）" % (ch, st))
            self._refresh_calinfo()
            for c in self.cards:
                c.hist.clear()
            self.chart.clear()
            return
        if isinstance(res, str):
            self._logline(res)
        if name.startswith("mode") or name.startswith("load"):
            m = name.split()[1]
            self.mode, self.nch = m, (3 if m == "f" else 6)
            self.var_mode.set(m)
            self._rebuild_cards()
            self.samples = 0
        if name.startswith("power") or name.startswith("load") or name.startswith("mode") \
                or name.startswith("speed") or name == "settings":
            self.worker.call("params", self.dev.params)
            self.worker.call("rail", self.dev.rail_mv)

    def _apply_params(self, p):
        if not p:
            return
        if "mode" in p:
            m = "f" if str(p["mode"]).upper().startswith("FLOAT") else "g"
            if m != self.mode:
                self.mode, self.nch = m, (3 if m == "f" else 6)
                self.var_mode.set(m)
                self._rebuild_cards()
        loaded = p.get("loaded", True)
        rate = F_OLF_HZ / (2 * p["conv_time"]) if p.get("conv_time") else 0
        self.st_cfg.config(text="固件 %s   C_REF_SEL=%s  C_AVRG=%s  CONV_TIME=%s (标称 %.1f Hz)  补偿 内%s 外%s"
                           % ("已载入" if loaded else "未载入", p.get("refsel", "?"), p.get("avrg", "?"),
                              p.get("conv_time", "?"), rate,
                              "✓" if p.get("comp_int") else "✗", "✓" if p.get("comp_ext") else "✗"))
        for k, (a, c) in SPEED_PRESETS.items():
            if p.get("avrg") == a and p.get("conv_time") == c:
                self.var_speed.set(SPEED_LABELS[k])

    def _on_sample(self, s):
        self.samples += 1
        now = time.time()
        if self.last_sample_t is not None:
            self.rate_hist.append(now - self.last_sample_t)
        self.last_sample_t = now
        unit, calib = self._units()
        pf = s.pf
        vals = []
        for i in range(self.nch):
            is_open = s.is_open(i)
            v = None if is_open else ((pf[i] if calib else s.channels[i]))
            if calib and self.chart.ymode == "ac" and v is not None:
                v = v * 1000.0                     # fF for the delta view
            vals.append(v)
            if i < len(self.cards):
                self.cards[i].update(s.channels[i], pf[i] if calib else None, is_open)
        self.chart.push(now, vals)
        if self.rec_writer:
            self.rec_writer.writerow(["%.6f" % s.host_t, s.t_ms] + [s.raw[i] for i in range(self.nch)]
                                     + ["%.9f" % s.channels[i] for i in range(self.nch)]
                                     + [("%.6f" % pf[i]) if pf[i] is not None else "" for i in range(self.nch)]
                                     + ["%02X" % v for v in s.status] + ["|".join(s.errors)])
            self.rec_count += 1
            if self.rec_count % 10 == 0:
                self.lbl_rec.config(text="%d 行" % self.rec_count)
        self.st_n.config(text="样本 %d" % self.samples)
        if len(self.rate_hist) >= 3:
            self.st_rate.config(text="速率 %.2f Hz" % (1.0 / (sum(self.rate_hist) / len(self.rate_hist))))
        if s.errors:
            self.st_err.config(text=", ".join(s.errors))
        elif self.samples % 50 == 0:
            self.st_err.config(text="")

    def _tick_chart(self):
        if self.worker and self.worker.streaming:
            self.chart.redraw()
        self.after(120, self._tick_chart)


def main():
    ap = argparse.ArgumentParser(description="PCAP04 board GUI")
    ap.add_argument("--sim", action="store_true", help="simulated device, no hardware needed")
    ap.add_argument("-p", "--port", default=None)
    a = ap.parse_args()
    App(sim=a.sim, port=a.port).mainloop()


if __name__ == "__main__":
    main()
