# Host tools

Python library, command line and GUI. Licence: **MIT**.

```bash
pip3 install -r requirements.txt
```

`pyserial` is the only hard requirement. `pyusb` + `libusb-package` are needed
for DFU flashing; `pyocd` only if you use a CMSIS-DAP probe over SWD.

## Files

| | |
|---|---|
| `pcap04.py` | the library: `PCap04`, `Sample`, `Calibration` |
| `pcap.py` | command line |
| `pcap_gui.py` | tkinter GUI with a strip chart; `--sim` needs no hardware |
| `pcapdfu.py` | USB DFU flasher, pure Python |
| `pcapflash.py` | SWD flasher via pyOCD (optional) |
| `swdlib.py` | shared pyOCD helpers |
| `characterisation/` | the sweeps behind `../docs/measurements.md` |

## Library

```python
from pcap04 import PCap04

with PCap04() as d:
    d.power(True)
    d.load('f')                     # or d.mode('g') to switch at runtime
    d.speed('precise')
    for s in d.stream(100):
        print(s.pf)                 # [ch0, ch1, ch2] in picofarads
        if s.errors:
            print(s.errors)
```

`Sample` exposes `.raw` `.ratios` `.channels` `.pf` `.status` `.errors`
`.por_flags` `.runbit` `.is_open(i)`.

## Three traps this code exists to avoid

**Console framing.** The firmware prints `> ` when ready. Some commands sit in a
500 ms delay mid-output, so waiting for a silent gap returns early and the *next*
command is written while the firmware is not listening — dropped with no error.
Everything here waits for the prompt.

**Duplicate samples.** Polling faster than the conversion rate re-reads the same
registers, and a naive standard deviation then comes out as exactly 0.
`stream()` is gated on INTN in firmware; `read()` de-duplicates on the raw word;
`stats()` raises rather than reporting zero variance.

**Stale data after a reset.** A PCAP04 power-on reset zeroes the config
registers but leaves the *result* registers holding the previous conversion, so
a dead chip hands back plausible numbers. `read()` checks `RUNBIT` and raises;
pass `allow_stale=True` if you want to inspect them anyway.

There is also an advisory lock on the serial port: two processes opening
`/dev/cu.*` on macOS does not error, it just interleaves bytes and looks exactly
like wedged hardware.

## Calibration

The chip reports `C / Cref`. One known capacitor turns that into picofarads:

```bash
python3 pcap.py calib 100 -c 2            # 100 pF on channel 2
python3 pcap.py calib 100 -c 2 --sweep    # characterise all 31 reference codes
```

Stored in `pcap04_calibration.json` (gitignored — it is specific to your board)
and loaded automatically afterwards.
