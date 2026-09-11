#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""pcapdfu.py - read/write firmware on the PCAP04 v1.5 board over USB DFU.

No debug probe required: the STM32G0B1 ROM bootloader speaks ST's DfuSe
protocol on the board's own USB-C connector.  A virgin chip enters it
automatically; a programmed chip needs 'pcapflash.py' over SWD, or the
BOOT0 pin, to get back in.

    ./pcapdfu.py info                    show the DFU device and its layout
    ./pcapdfu.py write fw.hex            erase + program + verify + run
    ./pcapdfu.py read dump.bin           read the whole flash back
    ./pcapdfu.py verify fw.hex           compare flash against a file
    ./pcapdfu.py erase                   mass erase
    ./pcapdfu.py run                     leave DFU and start the application
"""
import argparse, hashlib, os, struct, sys, time

VID, PID = 0x0483, 0xDF11
FLASH_ORIGIN = 0x08000000

# DFU class requests
DETACH, DNLOAD, UPLOAD, GETSTATUS, CLRSTATUS, GETSTATE, ABORT = range(7)
OUT_RT, IN_RT = 0x21, 0xA1
# DFU states
ST_IDLE, ST_DNLOAD_SYNC, ST_DNBUSY, ST_DNLOAD_IDLE = 2, 3, 4, 5
ST_MANIFEST_SYNC, ST_MANIFEST, ST_UPLOAD_IDLE, ST_ERROR = 6, 7, 9, 10
STATE_NAMES = {0: "appIDLE", 1: "appDETACH", 2: "dfuIDLE", 3: "dfuDNLOAD-SYNC",
               4: "dfuDNBUSY", 5: "dfuDNLOAD-IDLE", 6: "dfuMANIFEST-SYNC",
               7: "dfuMANIFEST", 8: "dfuMANIFEST-WAIT-RESET", 9: "dfuUPLOAD-IDLE",
               10: "dfuERROR"}
DFU_STATUS = {0x00: "OK", 0x01: "errTARGET", 0x02: "errFILE", 0x03: "errWRITE",
              0x04: "errERASE", 0x05: "errCHECK_ERASED", 0x06: "errPROG",
              0x07: "errVERIFY", 0x08: "errADDRESS", 0x09: "errNOTDONE",
              0x0A: "errFIRMWARE", 0x0F: "errUNKNOWN", 0x0E: "errSTALLEDPKT"}


def _backend():
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:
        return None


class Dfu:
    def __init__(self, intf=0, alt=0):
        import usb.core, usb.util
        self.usb_util = usb.util
        self.dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_backend())
        if self.dev is None:
            raise SystemExit(
                "no STM32 DFU device (0483:DF11) found.\n"
                "  - a virgin chip enters DFU by itself; plug the board's USB-C into this Mac\n"
                "  - once firmware is flashed it will NOT come back on its own:\n"
                "      use  ./pcapflash.py erase   over SWD to make it virgin again")
        self.intf, self.alt = intf, alt
        try:
            self.dev.set_configuration()
        except Exception:
            pass
        self.usb_util.claim_interface(self.dev, intf)
        self.dev.set_interface_altsetting(intf, alt)
        self.transfer_size, self.layout = self._read_functional()
        self.clear_errors()

    # -- descriptors -------------------------------------------------------
    def _read_functional(self):
        xfer, layout = 1024, None
        for cfg in self.dev:
            for i in cfg:
                if i.bInterfaceNumber != self.intf:
                    continue
                if i.iInterface:
                    try:
                        s = self.usb_util.get_string(self.dev, i.iInterface)
                        if s and s.startswith("@") and i.bAlternateSetting == self.alt:
                            layout = s
                    except Exception:
                        pass
                ex = bytes(i.extra_descriptors or b"")
                if len(ex) >= 9 and ex[1] == 0x21:
                    xfer = struct.unpack("<H", ex[5:7])[0]
        return xfer, layout

    def name(self):
        try:
            return (self.usb_util.get_string(self.dev, self.dev.iManufacturer),
                    self.usb_util.get_string(self.dev, self.dev.iProduct),
                    self.usb_util.get_string(self.dev, self.dev.iSerialNumber))
        except Exception:
            return ("?", "?", "?")

    # -- primitives --------------------------------------------------------
    def _out(self, req, wValue, data=b""):
        return self.dev.ctrl_transfer(OUT_RT, req, wValue, self.intf, data, 5000)

    def _in(self, req, wValue, length):
        return self.dev.ctrl_transfer(IN_RT, req, wValue, self.intf, length, 5000)

    def get_status(self):
        s = self._in(GETSTATUS, 0, 6)
        status = s[0]
        poll = s[1] | (s[2] << 8) | (s[3] << 16)
        state = s[4]
        return status, poll, state

    def get_state(self):
        return self._in(GETSTATE, 0, 1)[0]

    def clear_errors(self):
        for _ in range(4):
            st = self.get_state()
            if st == ST_IDLE:
                return
            if st == ST_ERROR:
                self._out(CLRSTATUS, 0)
            else:
                self._out(ABORT, 0)
            time.sleep(0.02)

    def _wait(self, what):
        """Poll GETSTATUS until the device leaves the busy states."""
        while True:
            status, poll, state = self.get_status()
            if status != 0:
                raise RuntimeError("%s failed: %s (state %s)"
                                   % (what, DFU_STATUS.get(status, hex(status)),
                                      STATE_NAMES.get(state, state)))
            if state in (ST_DNBUSY, ST_MANIFEST, ST_DNLOAD_SYNC, ST_MANIFEST_SYNC):
                time.sleep(max(poll, 1) / 1000.0)
                continue
            return state

    # -- DfuSe commands ----------------------------------------------------
    def set_address(self, addr):
        self._out(DNLOAD, 0, bytes([0x21]) + struct.pack("<I", addr))
        self._wait("set address 0x%08X" % addr)

    def erase_page(self, addr):
        self._out(DNLOAD, 0, bytes([0x41]) + struct.pack("<I", addr))
        self._wait("erase page 0x%08X" % addr)

    def mass_erase(self):
        self._out(DNLOAD, 0, bytes([0x41]))
        self._wait("mass erase")

    def write_block(self, index, data):
        self._out(DNLOAD, 2 + index, data)
        self._wait("write block %d" % index)

    def read_block(self, index, length):
        return bytes(self._in(UPLOAD, 2 + index, length))

    def leave(self, addr=FLASH_ORIGIN):
        """Set the address pointer then issue a zero-length download: the
        bootloader manifests and jumps to the application.  Every step here
        can legitimately fail with a USB pipe/IO error, because a successful
        jump tears the USB link down mid-transaction - that is not an error."""
        try:
            self.set_address(addr)
            self.clear_errors()
            self._out(DNLOAD, 0, b"")
            self._wait("leave DFU")
        except Exception:
            pass

    def close(self):
        try:
            self.usb_util.release_interface(self.dev, self.intf)
            self.usb_util.dispose_resources(self.dev)
        except Exception:
            pass


# ---------------------------------------------------------------- file I/O
def load_image(path):
    """Return (base_address, bytes) for a .bin or Intel .hex file."""
    if path.lower().endswith((".hex", ".ihex")):
        segs, base = {}, 0
        for line in open(path):
            line = line.strip()
            if not line.startswith(":"):
                continue
            n = int(line[1:3], 16); off = int(line[3:7], 16); typ = int(line[7:9], 16)
            data = bytes.fromhex(line[9:9 + 2 * n])
            if typ == 0:
                segs[base + off] = data
            elif typ == 4:
                base = int.from_bytes(data, "big") << 16
            elif typ == 2:
                base = int.from_bytes(data, "big") << 4
        if not segs:
            raise SystemExit("no data records in %s" % path)
        lo = min(segs); hi = max(a + len(d) for a, d in segs.items())
        buf = bytearray(b"\xff" * (hi - lo))
        for a, d in segs.items():
            buf[a - lo:a - lo + len(d)] = d
        return lo, bytes(buf)
    return FLASH_ORIGIN, open(path, "rb").read()


def human(n):
    return "%d bytes" % n if n < 1024 else "%.1f KB" % (n / 1024.0)


# ---------------------------------------------------------------- commands
def cmd_info(a, d):
    man, prod, ser = d.name()
    print("  device        : %s / %s" % (man, prod))
    print("  serial        : %s" % ser)
    print("  transfer size : %d bytes" % d.transfer_size)
    print("  memory layout : %s" % (d.layout or "unknown"))
    print("  state         : %s" % STATE_NAMES.get(d.get_state(), "?"))
    first = d.read_block(0, 4)
    print("  flash[0]      : 0x%08X %s"
          % (struct.unpack("<I", first)[0],
             "(erased - virgin)" if first == b"\xff\xff\xff\xff" else "(programmed)"))


def cmd_erase(a, d):
    print("mass erasing ...")
    t0 = time.time()
    d.mass_erase()
    print("  done in %.1f s" % (time.time() - t0))


def cmd_write(a, d):
    base, img = load_image(a.file)
    if base != FLASH_ORIGIN:
        print("note: image starts at 0x%08X" % base)
    print("programming %s  (%s)" % (a.file, human(len(img))))

    if a.mass_erase:
        print("  mass erase ...")
        d.mass_erase()
    else:
        page = 2048
        first = base & ~(page - 1)
        last = (base + len(img) + page - 1) & ~(page - 1)
        n = (last - first) // page
        print("  erasing %d pages of 2 KB ..." % n)
        for i in range(n):
            d.erase_page(first + i * page)
            print("\r    %d/%d" % (i + 1, n), end="", flush=True)
        print()

    print("  writing ...")
    d.clear_errors()
    d.set_address(base)
    xs = d.transfer_size
    nblk = (len(img) + xs - 1) // xs
    t0 = time.time()
    for i in range(nblk):
        d.write_block(i, img[i * xs:(i + 1) * xs])
        print("\r    %d/%d blocks" % (i + 1, nblk), end="", flush=True)
    print("\n  wrote %s in %.1f s" % (human(len(img)), time.time() - t0))

    if a.verify:
        print("  verifying ...")
        got = read_range(d, base, len(img))
        if got != img:
            bad = next(i for i in range(len(img)) if got[i] != img[i])
            raise SystemExit("VERIFY FAILED at 0x%08X (wrote 0x%02X, read 0x%02X)"
                             % (base + bad, img[bad], got[bad]))
        print("  verified OK  (sha256 %s)" % hashlib.sha256(img).hexdigest()[:16])

    if not a.no_run:
        print("  leaving DFU ...")
        d.leave(base)
        print()
        print("  !! IMPORTANT - unplug and replug the USB-C cable once. !!")
        print()
        print("  RM0444 2.5.4: FLASH_ACR.EMPTY is only re-evaluated at a POWER-ON reset")
        print("  or an option-byte reload.  A virgin chip that has just been programmed")
        print("  still thinks it is empty, so it keeps booting the ST bootloader until")
        print("  it sees a real power cycle.  After one unplug/replug it will run your")
        print("  firmware from then on.")


def read_range(d, addr, size):
    d.clear_errors()
    d.set_address(addr)
    # UPLOAD needs the device in dfuIDLE with the address pointer set
    d._out(ABORT, 0)
    d.get_status()
    xs = d.transfer_size
    out = bytearray()
    n = (size + xs - 1) // xs
    for i in range(n):
        out += d.read_block(i, min(xs, size - len(out)))
        print("\r    %d/%d blocks" % (i + 1, n), end="", flush=True)
    print()
    return bytes(out[:size])


def cmd_read(a, d):
    size = a.size or 128 * 1024
    print("reading %s from 0x%08X ..." % (human(size), a.addr))
    t0 = time.time()
    data = read_range(d, a.addr, size)
    open(a.file, "wb").write(data)
    print("  wrote %s in %.1f s" % (a.file, time.time() - t0))
    print("  sha256     : %s" % hashlib.sha256(data).hexdigest())
    print("  non-erased : %s" % human(len(data.rstrip(b"\xff"))))


def cmd_verify(a, d):
    base, img = load_image(a.file)
    print("verifying %s against flash ..." % a.file)
    got = read_range(d, base, len(img))
    print("  %s" % ("OK - flash matches" if got == img else "MISMATCH"))


def cmd_run(a, d):
    print("leaving DFU ...")
    d.leave()
    print("  running.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(fn=cmd_info)
    sub.add_parser("erase").set_defaults(fn=cmd_erase)
    sub.add_parser("run").set_defaults(fn=cmd_run)

    p = sub.add_parser("write"); p.set_defaults(fn=cmd_write); p.add_argument("file")
    p.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    p.add_argument("--mass-erase", action="store_true")
    p.add_argument("--no-run", action="store_true")

    p = sub.add_parser("verify"); p.set_defaults(fn=cmd_verify); p.add_argument("file")

    p = sub.add_parser("read"); p.set_defaults(fn=cmd_read); p.add_argument("file")
    p.add_argument("--addr", type=lambda s: int(s, 0), default=FLASH_ORIGIN)
    p.add_argument("--size", type=lambda s: int(s, 0), default=0)

    a = ap.parse_args()
    d = None
    try:
        d = Dfu()
        a.fn(a, d)
    except SystemExit:
        raise
    except Exception as e:
        print("\nERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        sys.exit(1)
    finally:
        if d:
            d.close()


if __name__ == "__main__":
    main()
