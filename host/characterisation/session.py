#!/usr/bin/env python3
"""session.py - one serial session, a list of commands, correct read framing.

Usage:  session.py "cmd1" "cmd2" ...
        session.py -f script.txt
Lines beginning with '#' are comments; a line '@sleep 2.0' waits.

Framing note: the console prints "> " when it is ready for the next line, and
some commands (loadf) sit in a 500 ms delay in the middle of their output.
Waiting for a silent gap therefore returns early and the NEXT command is
written while the firmware is not listening, which silently drops it.  Always
wait for the prompt.
"""
import sys, time, glob, serial

PROMPT = b"> "


def open_port():
    ports = sorted(glob.glob('/dev/cu.usbmodem*'))
    if not ports:
        sys.exit("no /dev/cu.usbmodem* found")
    t0 = time.time()
    s = serial.Serial(ports[0], 115200, timeout=0.05)
    dt = time.time() - t0
    if dt > 2.0:
        print("!! serial open took %.1f s - the hub is wedged, replug the board" % dt)
    time.sleep(0.2)
    s.reset_input_buffer()
    return s


def cmd(s, c, hard=15.0, quiet=1.0):
    """Send one line; read until the firmware prints its prompt again."""
    s.reset_input_buffer()
    s.write(c.encode() + b"\r\n")
    s.flush()
    buf, t_start, last = b"", time.time(), time.time()
    while time.time() - t_start < hard:
        d = s.read(4096)
        if d:
            buf += d
            last = time.time()
            if buf.rstrip(b"\r\n ").endswith(b">") or buf.endswith(PROMPT):
                break
        elif buf and (time.time() - last) > quiet:
            break                       # prompt never came; fall back on silence
    txt = buf.decode("utf-8", "replace")
    return "\n".join(l for l in txt.splitlines() if l.strip() not in ("", ">", c))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["-f"]:
        args = [l.rstrip() for l in open(args[1]) if l.strip() and not l.startswith("#")]
    s = open_port()
    for c in args:
        if c.startswith("@sleep"):
            time.sleep(float(c.split()[1])); continue
        print("$ " + c)
        print(cmd(s, c))
        print()
    s.close()
