#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
pcap04.py - host-side control of the PCAP04_Acquisition_3CH v1.5 board.

    from pcap04 import PCap04

    with PCap04() as d:
        d.power(True)               # U5 (TPS22918) load switch -> +3V3P
        d.load('f')                 # ScioSense standard firmware, floating
        for s in d.stream(100):
            print(s.pf)             # [ch0, ch1, ch2] in picofarads

Requires firmware v3.0.0 or later on the STM32 (`stream`, `mode`, `avrg`,
`conv`, `refsel`, `comp`, `ports`, `params`, `health`).

Three traps this module exists to keep you out of
--------------------------------------------------
1.  Console framing.  The firmware prints "> " when it is ready.  Some
    commands (`load`) sit in a 500 ms delay in the middle of their output,
    so waiting for a silent gap returns early and the NEXT command is
    written while the firmware is not listening - it is dropped with no
    error at all.  Everything here waits for the prompt.
2.  Duplicate samples.  Polling faster than the conversion rate re-reads the
    same result registers, and a naive standard deviation then comes out as
    exactly 0.  `stream()` is gated on the INTN pin in firmware, and
    `read()` de-duplicates on the raw 32-bit word.
3.  Silent misconfiguration.  A config register write only reaches the
    running conversion after INIT + CDC_START, and the first conversion
    after a restart is usually garbage.  Every setter here restarts and
    drops the settling samples.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import statistics
import sys
import time

try:
    import serial
    import serial.tools.list_ports as list_ports
except ImportError:                                          # pragma: no cover
    serial = list_ports = None   # only PCap04() needs it; Sample/Calibration do not

__all__ = ["PCap04", "Sample", "Calibration", "PCapError"]

Q527 = 2.0 ** 27
#: An unconnected floating pair does not sit at exactly 0x7FFFFFFF - it wanders
#: just below it (0x7FFFFF9F was seen).  Anything near the Q5.27 ceiling means
#: "nothing connected", so test the ratio, not the exact word.
SATURATED_RATIO = 15.9
SATURATED = int(SATURATED_RATIO * Q527)

#: floating mode measures PAIRS; grounded mode measures single electrodes
FLOATING_CHANNELS = {0: ("PC0/PC1", "J2/J3"),
                     1: ("PC2/PC3", "J4/J5"),
                     2: ("PC4/PC5", "J6/J7")}
GROUNDED_CHANNELS = {i: ("PC%d" % i, "J%d" % (i + 2)) for i in range(6)}

#: C_AVRG >= 512 measures ~19.5 % low on this board with a ~100 pF sensor.
#: 256 and 257 are both clean, so it is not a low-byte-zero effect - most
#: likely accumulator saturation with large capacitances.  The firmware
#: clamps too; this is here so the host can warn before the round trip.
C_AVRG_MAX = 256

#: measured, not assumed: CONV_TIME = 2000 gave 12.8 Hz
F_OLF_HZ = 51000.0

#: The board's own USB-C console (firmware >= 3.1.0).  pid.codes prototype ID.
#: Matching on this is what stops the tools grabbing the Debug Probe's port,
#: which is the other /dev/cu.usbmodem* on the same machine.
BOARD_VID, BOARD_PID = 0x1209, 0x0001

SPEED_PRESETS = {
    #  name        C_AVRG  CONV_TIME   what you get on a 100 pF sensor
    "fast":      (32,   2000),      # ~12.8 Hz, ~20 fF rms
    "balanced":  (128,  2000),      # ~12.8 Hz, ~6.4 fF rms
    "precise":   (256,  8000),      # ~3.3 Hz,  ~3.9 fF rms
}


class PCapError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# one conversion
# ----------------------------------------------------------------------------

class Sample:
    """One conversion: raw result registers plus the decoded status."""

    __slots__ = ("t_ms", "host_t", "raw", "status", "_cal", "_mode")

    def __init__(self, t_ms, raw, status, cal=None, mode="f"):
        self.t_ms = t_ms
        self.host_t = time.time()
        self.raw = list(raw)                 # 8 x uint32
        self.status = tuple(status)          # (s0, s1, s2)
        self._cal = cal
        self._mode = mode

    # -- raw ratios -----------------------------------------------------
    @property
    def ratios(self):
        return [r / Q527 for r in self.raw]

    @property
    def n_channels(self):
        return 3 if self._mode == "f" else 6

    @property
    def channels(self):
        """Ratios of the channels that this mode actually measures."""
        return self.ratios[:self.n_channels]

    @property
    def saturated(self):
        """Channels at full scale - almost always nothing connected."""
        return [i for i in range(self.n_channels) if self.is_open(i)]

    def is_open(self, i):
        return self.raw[i] >= SATURATED

    # -- calibrated -----------------------------------------------------
    @property
    def pf(self):
        """Channel capacitances in pF, or None where no calibration exists."""
        if self._cal is None:
            return [None] * self.n_channels
        return [self._cal.to_pf(i, r) for i, r in enumerate(self.channels)]

    # -- status ---------------------------------------------------------
    @property
    def runbit(self):
        return bool(self.status[0] & 0x01)

    @property
    def por_flags(self):
        """Why the PCAP04 last powered on.  These latch at the POR and stay set
        until the next one, so they describe history, not the current
        conversion - a blank chip sitting unprogrammed sets POR_FLAG_WDOG
        every ~10 s, and the flag then survives into a healthy session."""
        s0 = self.status[0]
        out = []
        if s0 & 0x80: out.append("POR_FLAG_WDOG")
        if s0 & 0x40: out.append("POR_FLAG_CONFIG")
        if s0 & 0x20: out.append("POR_CDC_DSP_COLL")
        return out

    @property
    def errors(self):
        """Faults affecting THIS conversion.  Sticky POR-cause flags are in
        .por_flags instead, and only surface here if the chip is also idle,
        which is what an unrecovered reset actually looks like."""
        s0, s1, s2 = self.status
        out = []
        if not (s0 & 0x01):
            out.append("RUNBIT_CLEAR")
            out += self.por_flags
        if s1 & 0x08: out.append("RDC_ERR")
        if s1 & 0x04: out.append("MUP_ERR")
        if s1 & 0x02: out.append("ERR_OVFL")
        if s2 & 0x40: out.append("PORT_ERR_INTREF")
        for i in range(6):
            if s2 & (1 << i):
                out.append("PORT_ERR_PC%d" % i)
        return out

    def __repr__(self):
        pf = self.pf
        if any(p is not None for p in pf):
            body = "  ".join("ch%d=%s" % (i, "%.4f pF" % p if p is not None else "--")
                             for i, p in enumerate(pf))
        else:
            body = "  ".join("ch%d=%.6f" % (i, r) for i, r in enumerate(self.channels))
        err = ("  [" + ",".join(self.errors) + "]") if self.errors else ""
        return "<Sample t=%d %s%s>" % (self.t_ms, body, err)


# ----------------------------------------------------------------------------
# calibration
# ----------------------------------------------------------------------------

class Calibration:
    """C = ratio * Cref.

    Cref is the on-chip reference capacitor and depends on C_REF_SEL.  The
    datasheet formula (0.959*N + 3.23 pF) does NOT describe the real part:
    this board measures ~0.975*N + 9.91 pF, i.e. about +7 pF of fixed
    parasitic, and the step width varies by up to 4 % from code to code.
    So Cref is stored per C_REF_SEL code, measured, never computed.

    `stray` is the trace-to-ground capacitance that external compensation
    removes in floating mode.  It is recorded for information; with
    C_COMP_EXT on it is already out of the reading.
    """

    DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "pcap04_calibration.json")

    def __init__(self, cref=None, stray=None, refsel=31, note=""):
        self.cref = dict(cref or {})        # {refsel(int): pF}
        self.stray = dict(stray or {})      # {channel(int): pF}
        self.refsel = refsel
        self.note = note

    # -- use ------------------------------------------------------------
    def cref_pf(self, refsel=None):
        n = self.refsel if refsel is None else refsel
        return self.cref.get(int(n))

    def to_pf(self, channel, ratio, refsel=None):
        c = self.cref_pf(refsel)
        return None if c is None else ratio * c

    # -- persistence ----------------------------------------------------
    def save(self, path=None):
        path = path or self.DEFAULT_PATH
        with open(path, "w") as f:
            json.dump({"cref_pf_by_refsel": {str(k): v for k, v in self.cref.items()},
                       "stray_pf_by_channel": {str(k): v for k, v in self.stray.items()},
                       "refsel": self.refsel,
                       "note": self.note,
                       "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)
        return path

    @classmethod
    def load(cls, path=None):
        path = path or cls.DEFAULT_PATH
        if not os.path.exists(path):
            return None
        with open(path) as f:
            d = json.load(f)
        return cls(cref={int(k): v for k, v in d.get("cref_pf_by_refsel", {}).items()},
                   stray={int(k): v for k, v in d.get("stray_pf_by_channel", {}).items()},
                   refsel=d.get("refsel", 31),
                   note=d.get("note", ""))

    def __repr__(self):
        return "<Calibration refsel=%s Cref=%s pF stray=%s>" % (
            self.refsel,
            "%.3f" % self.cref_pf() if self.cref_pf() else "?",
            {k: round(v, 2) for k, v in self.stray.items()})


# ----------------------------------------------------------------------------
# the board
# ----------------------------------------------------------------------------

class PCap04:
    PROMPT = b"> "

    def __init__(self, port=None, baud=115200, calibration="auto", verbose=False):
        self.verbose = verbose
        if serial is None:
            raise PCapError("pyserial is missing:  pip3 install pyserial")
        self.port_name = port or self._find_port()
        self.via_usb = any(d == self.port_name and b for d, b in self._list_candidates())
        # Two processes CAN both open /dev/cu.* on macOS.  Neither gets an
        # error; they just interleave bytes, the console appears to answer
        # nothing, and opening takes tens of seconds - which looks exactly
        # like the USB hub wedging after a DFU re-enumeration.  Take an
        # advisory lock so the second one says what is really wrong.
        self._lock = self._take_lock(self.port_name)
        t0 = time.time()
        self.ser = serial.Serial(self.port_name, baud, timeout=0.05)
        self.open_seconds = time.time() - t0
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self._mode = "f"
        self._loaded = False
        if calibration == "auto":
            self.cal = Calibration.load()
        elif isinstance(calibration, Calibration) or calibration is None:
            self.cal = calibration
        else:
            self.cal = Calibration.load(calibration)
        if self.open_seconds > 2.0:
            if self.via_usb:
                sys.stderr.write(
                    "!! opening %s took %.1f s.  That is the board's own USB\n"
                    "   console, so this is not the Debug Probe hub problem.  A\n"
                    "   consistent ~50 s means the firmware is not completing the\n"
                    "   status stage of SET_LINE_CODING - fixed in 3.1.0, so check\n"
                    "   'info' reports 3.1.0 or later.\n"
                    % (self.port_name, self.open_seconds))
            else:
                sys.stderr.write(
                    "!! opening %s took %.1f s.  This is the Debug Probe's port;\n"
                    "   on macOS it shares a hub with the board's USB-C and a DFU\n"
                    "   re-enumeration wedges its CDC interface.  Replug the probe,\n"
                    "   or just use the board's own USB-C console instead.\n"
                    % (self.port_name, self.open_seconds))

    # -- plumbing -------------------------------------------------------
    @staticmethod
    def _take_lock(port):
        path = os.path.join("/tmp", "pcap04" + port.replace("/", "_") + ".lock")
        fh = open(path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise PCapError(
                "%s is already open by another pcap04 process.\n"
                "  Two readers on one serial port do not error - they interleave\n"
                "  bytes and the console goes silent.  Close the other one\n"
                "  (pkill -f pcap.py) and retry." % port)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh

    @staticmethod
    def _list_candidates():
        """(device, is_board) for every plausible port, board's own USB first.

        Before firmware 3.1.0 the only console was the UART behind the Debug
        Probe, so the tools just took the first usbmodem.  With the board now
        presenting its own CDC port there are two, and picking the wrong one
        gets silence.  Match on VID/PID."""
        out = []
        if list_ports is not None:
            for p in list_ports.comports():
                if p.vid == BOARD_VID and p.pid == BOARD_PID:
                    out.append((p.device, True))
            for p in list_ports.comports():
                if not (p.vid == BOARD_VID and p.pid == BOARD_PID):
                    if "usbmodem" in p.device or "ttyACM" in p.device or p.device.startswith("COM"):
                        out.append((p.device, False))
        if not out:
            out = [(d, False) for d in sorted(glob.glob("/dev/cu.usbmodem*") +
                                              glob.glob("/dev/ttyACM*") +
                                              glob.glob("/dev/tty.usbmodem*"))]
        return out

    @classmethod
    def _find_port(cls):
        cands = cls._list_candidates()
        if not cands:
            raise PCapError(
                "no serial port found.  Either plug the board's USB-C into this\n"
                "  machine (firmware 3.1.0+ presents its own console), or connect\n"
                "  the Debug Probe's U port to J10.")
        return cands[0][0]

    def cmd(self, line, hard=20.0, quiet=1.5, _retry=True):
        """Send one command line; read until the firmware prints its prompt."""
        self.ser.reset_input_buffer()
        self.ser.write(line.encode() + b"\r\n")
        self.ser.flush()
        buf, t_start, last = b"", time.time(), time.time()
        while time.time() - t_start < hard:
            d = self.ser.read(4096)
            if d:
                buf += d
                last = time.time()
                if buf.rstrip(b"\r\n ").endswith(b">"):
                    break
            elif buf and (time.time() - last) > quiet:
                break
        txt = buf.decode("utf-8", "replace")
        out = "\n".join(l for l in txt.splitlines()
                        if l.strip() not in ("", ">", line.strip()))
        if self.verbose:
            print("$ %s\n%s" % (line, out))
        # "ERR SPI buffer tri-stated" is the refusal; the identical phrase
        # without the ERR prefix is `bus off` REPORTING SUCCESS.  Matching the
        # bare phrase turns `bus off` into an infinite bus-on/bus-off loop.
        if _retry and out.lstrip().startswith("ERR SPI buffer tri-stated"):
            self.cmd("bus on", _retry=False)
            return self.cmd(line, hard=hard, quiet=quiet, _retry=False)
        if not txt.strip():
            raise PCapError(
                "no response to %r.  The serial link is wedged - replug the "
                "Debug Probe." % line)
        return out

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass
        try:
            lock = getattr(self, "_lock", None)
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- power: U5 (TPS22918 load switch) --------------------------------
    def power(self, on=True):
        """Switch +3V3P, the PCAP04 supply, via U5.  Also enables the SPI
        buffer (U6) so the bus is not left driving an unpowered chip."""
        if on:
            out = self.cmd("rail on")
            self.cmd("bus on")
        else:
            self.cmd("bus off")         # tri-state before removing the rail
            out = self.cmd("rail off")
        # Either transition wipes the PCAP04's SRAM.  The firmware invalidates
        # its own flag too (3.3.0+), but clear ours so a stale cache can never
        # let the next read through on a blank chip.
        self._loaded = False
        self._last_raw = None
        return out

    def power_cycle(self, off_ms=200):
        """Full power cycle.  The +3V3P rail discharges through RQOD1 (470 R)
        plus the switch's own 25 R into 20 uF, measured at ~43 ms, so the
        default 200 ms is comfortable."""
        self.power(False)
        time.sleep(off_ms / 1000.0)
        return self.power(True)

    def rail_mv(self):
        out = self.cmd("adc")
        m = re.search(r"\+3V3P\s*=\s*([\d.]+)\s*V", out)
        return float(m.group(1)) * 1000.0 if m else None

    # -- PCAP04 firmware -------------------------------------------------
    def load(self, mode="f"):
        """Write the 548-byte ScioSense standard firmware into the PCAP04's
        SRAM and configure it.  Needed after every PCAP04 reset: the chip's
        NVRAM is blank, so nothing survives a power cycle or a watchdog POR."""
        mode = self._norm_mode(mode)
        out = self.cmd("loadf" if mode == "f" else "load", hard=25.0)
        if "FAIL" in out or "ERR" in out:
            raise PCapError("load failed:\n" + out)
        self._mode = mode
        self._loaded = True
        return out

    def mode(self, m=None):
        """Switch grounded <-> floating without re-uploading the firmware
        (~30 ms instead of ~3 s).  Call with no argument to query."""
        if m is None:
            return self._mode
        m = self._norm_mode(m)
        self._ensure_loaded()
        if not self._loaded:
            return self.load(m)
        out = self.cmd("mode %s" % m)
        self._mode = m
        return out

    @staticmethod
    def _norm_mode(m):
        m = str(m).lower()[:1]
        if m in ("f", "g"):
            return m
        raise ValueError("mode must be 'f' (floating) or 'g' (grounded)")

    # -- front-end parameters --------------------------------------------
    def set_avrg(self, n):
        if n > C_AVRG_MAX:
            sys.stderr.write("C_AVRG %d exceeds the %d proven good on this board "
                             "(>=512 reads ~19.5%% low); it will be clamped.\n"
                             % (n, C_AVRG_MAX))
        return self.cmd("avrg %d" % n)

    def set_conv(self, n):
        return self.cmd("conv %d" % n)

    def set_discharge(self, n):
        """DISCHARGE_TIME.  The ScioSense default is 0, which makes any sensor
        with series resistance or a lossy dielectric report C_PortError and
        read full scale - it looks like a dead channel.  Firmware 3.2.0
        defaults to 8; raise it if a port still errors, but above ~8 the
        conversion stops fitting one trigger period and the rate halves."""
        return self.cmd("disch %d" % n)

    def set_refsel(self, n):
        out = self.cmd("refsel %d" % n)
        if self.cal:
            self.cal.refsel = int(n)
        return out

    def set_comp(self, internal=True, external=True):
        return self.cmd("comp %d" % ((1 if internal else 0) | (2 if external else 0)))

    def set_ports(self, mask):
        return self.cmd("ports %02X" % (mask & 0x3F))

    def speed(self, preset):
        """'fast' | 'balanced' | 'precise' - see SPEED_PRESETS."""
        if preset not in SPEED_PRESETS:
            raise ValueError("preset must be one of %s" % list(SPEED_PRESETS))
        self._ensure_loaded()      # a reload later would undo what we set here
        avrg, conv = SPEED_PRESETS[preset]
        self.set_avrg(avrg)
        return self.set_conv(conv)

    def params(self):
        out = self.cmd("params")
        d = {"raw": out}
        for key, pat in (("mode", r"mode\s*:\s*(\w+)"),
                         ("refsel", r"C_REF_SEL\s*=\s*(\d+)"),
                         ("avrg", r"C_AVRG\s*:\s*(\d+)"),
                         ("conv_time", r"CONV_TIME\s*:\s*(\d+)"),
                         ("discharge", r"DISCHARGE_TIME\s*:\s*(\d+)"),
                         ("port_en", r"C_PORT_EN\s*:\s*0x([0-9A-Fa-f]+)")):
            m = re.search(pat, out)
            if m:
                d[key] = m.group(1)
        for k in ("refsel", "avrg", "conv_time", "discharge"):
            if k in d:
                d[k] = int(d[k])
        if "port_en" in d:
            d["port_en"] = int(d["port_en"], 16)
        m = re.search(r"C_COMP_INT / C_COMP_EXT\s*:\s*(\d)\s*/\s*(\d)", out)
        if m:
            d["comp_int"], d["comp_ext"] = m.group(1) == "1", m.group(2) == "1"
        d["watchdog_off"] = "watchdog off" in out
        d["loaded"] = ("NOT loaded" not in out) and ("SRAM : loaded" in out)
        self._loaded = d["loaded"]
        # Only adopt the chip's mode when there IS firmware behind it; a blank
        # chip reads cfg 0x04 = 0, which would silently flip us to grounded.
        if "mode" in d and d.get("loaded"):
            self._mode = "f" if d["mode"].upper().startswith("FLOAT") else "g"
        return d

    def health(self):
        out = self.cmd("health")
        return {"raw": out,
                "runbit": "RUNBIT CLEAR" not in out,
                "errors": [w for w in ("POR_FLAG_WDOG", "POR_FLAG_CONFIG",
                                       "POR_CDC_DSP_COLL", "RDC_ERR", "MUP_ERR",
                                       "ERR_OVFL", "COMB_ERR") if w in out],
                "port_errors": re.findall(r"PortErr\(([^)]+)\)", out)}

    # -- reading ----------------------------------------------------------
    _RES_RE = re.compile(r"RES(\d) = 0x([0-9A-F]{8})")
    _ST_RE = re.compile(r"status 0x20\.\.0x22 = ([0-9A-F]{2}) ([0-9A-F]{2}) ([0-9A-F]{2})")

    def _parse_resd(self, out):
        raw = [0] * 8
        for m in self._RES_RE.finditer(out):
            raw[int(m.group(1))] = int(m.group(2), 16)
        st = self._ST_RE.search(out)
        if not st:
            raise PCapError("could not parse a result frame:\n" + out)
        return raw, tuple(int(g, 16) for g in st.groups())

    def _ensure_loaded(self):
        """Is the ScioSense firmware in the PCAP04's SRAM?

        That lives in the CHIP, not in this process, so a fresh host process
        must ask rather than assume.  Assuming "not loaded" makes every new
        process re-upload, and a re-upload rewrites the whole config from the
        ScioSense defaults - silently undoing any avrg/conv/refsel the caller
        just set."""
        # Deliberately re-query rather than trusting the cached flag: the chip
        # can reset on its own (watchdog POR) with the host none the wiser.
        # params() now reports the chip's RUNBIT, not a stale firmware flag.
        if not self.params().get("loaded"):
            self.load(self._mode)

    def read(self, fresh=True, tries=40, allow_stale=False):
        """One conversion.

        Two ways this can silently lie, both guarded here:
        - polling faster than the conversion rate re-reads the same registers,
          so with fresh=True we keep going until they actually change;
        - after any PCAP04 reset (rail cycle, or its own watchdog) the config
          registers are zeroed but the RESULT registers keep their last
          contents, so a dead chip hands back the previous measurement looking
          entirely plausible.  RUNBIT is the chip's own answer, so trust that.
        """
        last = getattr(self, "_last_raw", None)
        for _ in range(max(1, tries)):
            raw, st = self._parse_resd(self.cmd("resd"))
            s = Sample(0, raw, st, self.cal, self._mode)
            if not s.runbit and not allow_stale:
                self._loaded = False
                raise PCapError(
                    "the PCAP04 has been reset - RUNBIT is clear and its config "
                    "registers are zeroed.\n  The result registers still hold the "
                    "PREVIOUS measurement, so these numbers are stale, not live.\n"
                    "  Reload with load()/`--reload`, or pass allow_stale=True to "
                    "inspect them anyway.")
            if not fresh or last is None or raw != last:
                self._last_raw = raw
                return s
        raise PCapError("the result registers did not change in %d polls - the "
                        "PCAP04 has stopped (check .health())" % tries)

    _D_RE = re.compile(r"^D\s+(\d+)((?:\s+[0-9A-F]{8}){8})\s+"
                       r"([0-9A-F]{2})\s+([0-9A-F]{2})\s+([0-9A-F]{2})\s*$")

    def stream(self, n=0, timeout=10.0):
        """Yield one Sample per conversion.

        The firmware gates each frame on the INTN falling edge, so frames are
        distinct conversions by construction and arrive at the chip's own
        rate rather than at the console round-trip rate.  n=0 streams until
        the generator is closed."""
        self._ensure_loaded()
        self.ser.reset_input_buffer()
        self.ser.write(("stream %d\r\n" % n).encode())
        self.ser.flush()
        buf, last, count = b"", time.time(), 0
        try:
            while True:
                d = self.ser.read(4096)
                if d:
                    buf += d
                    last = time.time()
                elif time.time() - last > timeout:
                    raise PCapError("stream stalled for %.0f s" % timeout)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.decode("utf-8", "replace").strip()
                    if not line or line.startswith(">"):
                        continue
                    if line.startswith("#"):
                        if self.verbose:
                            print(line)
                        if "stopping" in line or "frames" in line:
                            return
                        continue
                    m = self._D_RE.match(line)
                    if not m:
                        continue
                    raw = [int(x, 16) for x in m.group(2).split()]
                    st = tuple(int(m.group(i), 16) for i in (3, 4, 5))
                    yield Sample(int(m.group(1)), raw, st, self.cal, self._mode)
                    count += 1
                    if n and count >= n:
                        return
        finally:
            self.ser.write(b"\r\n")     # any byte stops the firmware's loop
            self.ser.flush()
            time.sleep(0.3)
            self.ser.reset_input_buffer()

    def collect(self, n=30, timeout=25.0, settle=2):
        """n samples as a list.

        The first conversions after any INIT + CDC_START are still settling -
        the very first often reads full scale - so `settle` of them are taken
        and thrown away.  Every caller here has just changed a setting, so
        this is the common case rather than the exception."""
        got = list(self.stream(n + settle, timeout=timeout))
        return got[settle:] if len(got) > settle else got

    # -- statistics --------------------------------------------------------
    @staticmethod
    def stats(samples, channel=0):
        """mean / sd / peak-to-peak of one channel's ratio, plus the measured
        sample rate.  Raises if every value is identical, which always means
        duplicate samples rather than a noiseless measurement."""
        if any(s.is_open(channel) for s in samples):
            raise PCapError("channel %d reads full scale - nothing connected"
                            % channel)
        v = [s.channels[channel] for s in samples]
        if len(v) < 2:
            raise PCapError("need at least 2 samples")
        if len(set(v)) == 1:
            raise PCapError("all %d samples are bit-identical - these are "
                            "re-reads of one conversion, not %d measurements"
                            % (len(v), len(v)))
        span = samples[-1].host_t - samples[0].host_t
        return {"n": len(v),
                "mean": statistics.mean(v),
                "sd": statistics.stdev(v),
                "pp": max(v) - min(v),
                "ppm": statistics.stdev(v) / statistics.mean(v) * 1e6,
                "rate_hz": (len(v) - 1) / span if span > 0 else float("nan")}

    # -- calibration --------------------------------------------------------
    def calibrate(self, known_pf, channel=2, refsel=None, n=16, sweep=False,
                  measure_stray=True):
        """Calibrate against a capacitor of known value on one channel.

        Cref = known_pf / ratio, measured - never taken from the datasheet
        formula, which is out by ~7 pF on this part.

        sweep=True repeats the measurement for every C_REF_SEL code so the
        whole reference bank is characterised in one go; that matters because
        the step width varies by up to 4 % between codes.

        measure_stray=True additionally reads the channel with external
        compensation off, giving the trace-to-ground capacitance.  Floating
        mode only - C_COMP_EXT is not legal when grounded.
        """
        if self._mode != "f" and measure_stray:
            measure_stray = False
        codes = list(range(1, 32)) if sweep else [refsel if refsel is not None
                                                  else self.params().get("refsel", 31)]
        cal = self.cal or Calibration()

        for code in codes:
            self.set_refsel(code)
            self.set_comp(internal=True, external=(self._mode == "f"))
            s = self.collect(n)
            ratio = statistics.mean([x.channels[channel] for x in s])
            if ratio <= 0:
                continue
            cal.cref[int(code)] = known_pf / ratio

        if measure_stray:
            self.set_comp(internal=True, external=True)
            on = statistics.mean([x.channels[channel] for x in self.collect(n)])
            self.set_comp(internal=True, external=False)
            off = statistics.mean([x.channels[channel] for x in self.collect(n)])
            self.set_comp(internal=True, external=True)
            cal.stray[int(channel)] = known_pf * (off / on - 1.0)

        cal.refsel = int(codes[-1])
        cal.note = ("channel %d against %.4f pF, %s mode"
                    % (channel, known_pf, "floating" if self._mode == "f" else "grounded"))
        self.cal = cal
        return cal


if __name__ == "__main__":
    with PCap04(verbose=True) as dev:
        dev.power(True)
        dev.load("f")
        print(dev.params())
        for smp in dev.stream(5):
            print(smp)
