#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""pcapflash.py - read/write firmware on the PCAP04 v1.5 board over SWD.

Target : STM32G0B1CBT6 (128 KB flash @ 0x08000000)
Probe  : any CMSIS-DAP probe; tested with the Raspberry Pi Debug Probe.

    ./pcapflash.py info                     probe + target identity
    ./pcapflash.py write fw.hex             erase, program, verify, run
    ./pcapflash.py read  dump.bin           read the whole flash back
    ./pcapflash.py verify fw.hex            compare flash against a file
    ./pcapflash.py erase                    mass erase (back to virgin state)
    ./pcapflash.py reset [--halt]           reset the MCU
    ./pcapflash.py optr                     decode the option bytes
    ./pcapflash.py unempty                  clear the EMPTY flag (see below)

The EMPTY flag: a blank STM32G0 boots the ST system bootloader instead of your
code.  RM0444 2.5.4 says the flag is only re-evaluated on a POWER-ON reset or
an option-byte reload - a plain reset is not enough.  After the first
programming of a virgin chip this tool therefore performs an option-byte
reload automatically, so you do not have to unplug the board.
"""
import argparse, hashlib, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swdlib import (Board, FLASH_ORIGIN, FLASH_ACR, FLASH_ACR_EMPTY, FLASH_CR,
                    FLASH_KEYR, FLASH_OPTKEYR, FLASH_CR_OBL_LAUNCH,
                    FLASH_CR_OPTLOCK, FLASH_CR_LOCK, KEY1, KEY2,
                    OPTKEY1, OPTKEY2, FLASH_OPTR, decode_optr, fmt_uid, TARGET)

RESET_CAUSE = [(31,"low-power"),(30,"window watchdog"),(29,"independent watchdog"),
               (28,"software"),(27,"power-on / BOR"),(26,"NRST pin"),(25,"option byte load")]


def show_identity(b):
    i = b.identify()
    print("  target        : %s" % TARGET)
    print("  DBGMCU_IDCODE : 0x%08X   DEV_ID=0x%03X  REV=0x%04X  %s"
          % (i["idcode"], i["dev_id"], i["rev_id"],
             "OK (STM32G0B1)" if i["is_g0b1"] else "!! UNEXPECTED DEVICE"))
    print("  unique ID     : %s" % fmt_uid(i["uid"]))
    print("  flash size    : %d KB" % i["flash_kb"])
    o = decode_optr(i["optr"])
    print("  FLASH_OPTR    : 0x%08X  RDP=0x%02X%s nBOOT_SEL=%d nBOOT0=%d nBOOT1=%d"
          % (o["raw"], o["RDP"], " (level 0, unlocked)" if o["RDP"] == 0xAA else " (PROTECTED!)",
             o["nBOOT_SEL"], o["nBOOT0"], o["nBOOT1"]))
    empty = bool(i["acr"] & FLASH_ACR_EMPTY)
    print("  FLASH_ACR     : 0x%08X  EMPTY=%d %s"
          % (i["acr"], empty, "(virgin -> boots ST bootloader)" if empty else "(will boot your code)"))
    print("  flash[0]      : 0x%08X %s"
          % (i["flash0"], "(erased)" if i["flash0"] == 0xFFFFFFFF else "(programmed)"))
    causes = [n for bit, n in RESET_CAUSE if i["csr"] >> bit & 1]
    print("  last reset    : %s" % (", ".join(causes) if causes else "flags cleared"))
    return i


def unlock_flash(b):
    if b.rd(FLASH_CR) & FLASH_CR_LOCK:
        b.wr(FLASH_KEYR, KEY1); b.wr(FLASH_KEYR, KEY2)
    if b.rd(FLASH_CR) & FLASH_CR_OPTLOCK:
        b.wr(FLASH_OPTKEYR, OPTKEY1); b.wr(FLASH_OPTKEYR, OPTKEY2)


def obl_launch(b):
    """Reload the option bytes.  This re-evaluates EMPTY and resets the chip,
    so the debug link drops - that is expected, not an error."""
    unlock_flash(b)
    try:
        b.setbits(FLASH_CR, FLASH_CR_OBL_LAUNCH)
    except Exception:
        pass          # the reset tears the connection down mid-transaction
    time.sleep(0.3)


def cmd_info(args, b):
    print("probe           : %s (%s)" % (b.session.probe.product_name, b.session.probe.unique_id))
    show_identity(b)


def cmd_optr(args, b):
    o = decode_optr(b.rd(FLASH_OPTR))
    for k in ("raw", "RDP", "nBOOT_SEL", "nBOOT0", "nBOOT1", "NRST_MODE"):
        print("  %-10s = %s" % (k, hex(o[k]) if k == "raw" else o[k]))
    print("\n  nBOOT_SEL=1 means the BOOT0 *pin* (J9-3) is ignored and the nBOOT0")
    print("  option bit decides;  nBOOT0=1 -> boot from main flash.  That is the")
    print("  factory default and is what you want for SWD work.")


def cmd_erase(args, b):
    print("mass erasing ...")
    b.t.halt()
    b.t.mass_erase()
    print("  done - the chip is now virgin again (flash[0]=0x%08X)" % b.rd(FLASH_ORIGIN))


def cmd_write(args, b):
    from pyocd.flash.file_programmer import FileProgrammer
    path = args.file
    if not os.path.exists(path):
        raise SystemExit("no such file: %s" % path)
    fmt = "hex" if path.lower().endswith((".hex", ".ihex")) else "bin"
    was_empty = bool(b.rd(FLASH_ACR) & FLASH_ACR_EMPTY)
    print("programming %s (%s, %d bytes)%s"
          % (path, fmt, os.path.getsize(path), "  [chip was virgin]" if was_empty else ""))
    b.t.halt()
    prog = FileProgrammer(b.session, chip_erase=("chip" if args.chip_erase else "sector"),
                          progress=None, smart_flash=not args.no_smart)
    kw = {"base_address": FLASH_ORIGIN} if fmt == "bin" else {}
    prog.program(path, file_format=fmt, **kw)
    print("  programmed OK")

    if args.verify:
        ok = verify_file(b, path, fmt)
        if not ok:
            raise SystemExit("VERIFY FAILED")
        print("  verified OK")

    if was_empty and not args.no_obl:
        print("  chip was virgin -> reloading option bytes to clear the EMPTY flag")
        obl_launch(b)
        print("  (the debug link was reset by the option-byte reload, this is normal)")
    elif not args.no_run:
        b.t.reset()
    print("done.")


def read_flash(b, addr, size):
    return bytes(b.t.read_memory_block8(addr, size))


def verify_file(b, path, fmt):
    if fmt == "bin":
        want = open(path, "rb").read()
        got = read_flash(b, FLASH_ORIGIN, len(want))
        return got == want
    # intel hex
    segs = {}
    base = 0
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
    ok = True
    for a, d in sorted(segs.items()):
        got = read_flash(b, a, len(d))
        if got != d:
            print("  MISMATCH at 0x%08X" % a)
            ok = False
    return ok


def cmd_verify(args, b):
    fmt = "hex" if args.file.lower().endswith((".hex", ".ihex")) else "bin"
    print("verifying %s ..." % args.file)
    print("  %s" % ("OK - flash matches the file" if verify_file(b, args.file, fmt) else "MISMATCH"))


def cmd_read(args, b):
    size = args.size if args.size else b.flash_kb() * 1024
    print("reading %d bytes from 0x%08X ..." % (size, args.addr))
    t0 = time.time()
    data = read_flash(b, args.addr, size)
    open(args.file, "wb").write(data)
    used = len(data.rstrip(b"\xff"))
    print("  wrote %s  (%d bytes in %.1f s)" % (args.file, len(data), time.time() - t0))
    print("  sha256      : %s" % hashlib.sha256(data).hexdigest())
    print("  non-erased  : %d bytes (the rest is 0xFF)" % used)


def cmd_reset(args, b):
    if args.halt:
        b.t.reset_and_halt(); print("reset and halted")
    else:
        b.t.reset(); print("reset, running")


def cmd_unempty(args, b):
    if not (b.rd(FLASH_ACR) & FLASH_ACR_EMPTY):
        print("EMPTY is already clear - the chip boots your code."); return
    print("EMPTY is set; reloading option bytes ...")
    obl_launch(b)
    print("done (link reset is expected).")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--freq", type=int, default=1_000_000, help="SWD clock in Hz")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(fn=cmd_info)
    sub.add_parser("optr").set_defaults(fn=cmd_optr)
    sub.add_parser("erase").set_defaults(fn=cmd_erase)
    sub.add_parser("unempty").set_defaults(fn=cmd_unempty)

    p = sub.add_parser("write"); p.set_defaults(fn=cmd_write)
    p.add_argument("file")
    p.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    p.add_argument("--chip-erase", action="store_true", help="mass erase instead of per-sector")
    p.add_argument("--no-smart", action="store_true", help="always rewrite, even if unchanged")
    p.add_argument("--no-obl", action="store_true", help="do not reload option bytes")
    p.add_argument("--no-run", action="store_true", help="leave the MCU halted")

    p = sub.add_parser("verify"); p.set_defaults(fn=cmd_verify); p.add_argument("file")

    p = sub.add_parser("read"); p.set_defaults(fn=cmd_read)
    p.add_argument("file")
    p.add_argument("--addr", type=lambda s: int(s, 0), default=FLASH_ORIGIN)
    p.add_argument("--size", type=lambda s: int(s, 0), default=0)

    p = sub.add_parser("reset"); p.set_defaults(fn=cmd_reset)
    p.add_argument("--halt", action="store_true")

    args = ap.parse_args()
    try:
        with Board(freq=args.freq, verbose=args.verbose) as b:
            args.fn(args, b)
    except SystemExit:
        raise
    except Exception as e:
        print("\nERROR: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        if "No ACK" in str(e) or "not found" in str(e).lower():
            print("\nhints:\n"
                  "  - is the board powered?  J11 needs a jumper on pins 1-2 for USB power\n"
                  "  - J9 pin 1 to pin 5 should read 3.3 V\n"
                  "  - orange=SWCLK -> J9-3, yellow=SWDIO -> J9-2, black=GND -> J9-5\n"
                  "  - J9 pin 5 (GND) is the one nearest the bottom edge of the board\n",
                  file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
