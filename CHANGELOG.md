# Changelog

Versions refer to the STM32 firmware; the host tools track it.

## Unreleased (host tools only)

- Fix: `pcap.py log` included the first, unsettled conversion in its summary
  statistics. One such sample inflated the reported sd from ~40 ppm to
  ~2500 ppm, and since `drift` was simply the last value minus the first, it
  also reported a drift of several tenths of a pF across a run that was in fact
  flat. The CSV still records every sample; only the statistics now skip the
  first two, matching what `PCap04.collect()` already did.

## 3.3.0

- Fix: the "PCAP04 firmware is loaded" flag was never cleared, so after a power
  cycle the board still reported `loaded` while every config register read 0.
  Because a PCAP04 power-on reset zeroes the config registers but *leaves the
  result registers holding the previous conversion*, a dead chip returned
  plausible-looking stale data. `rail on`/`rail off`/`safe_shutdown` now clear
  it, and the state is verified against the chip's own `RUNBIT` rather than
  trusted from a cached flag — which also catches a watchdog reset.
- Fix: `params` no longer infers the mode from a zeroed config register on a
  blank chip (it used to silently report GROUNDED and switch the host to a
  six-channel interpretation of nothing).
- Host `read()` raises on `RUNBIT` clear instead of returning stale values;
  `collect()`/`stream()` reload automatically.
- Fix: a regression introduced while making the above change — the verification
  read was placed at the start of `cmd_results()`, and that extra SSN edge makes
  the chip treat the previous value as consumed (`EN_ASYNC_RD` is on in the
  standard config), so the result registers could update mid-read. Now uses the
  `RUNBIT` bit in `STATUS_0`, which that command already reads.

## 3.2.0

- `DISCHARGE_TIME` now defaults to 8 instead of the ScioSense reference value of
  0. Zero works for a C0G part on a short trace but makes any sensor with series
  resistance or a lossy dielectric report `C_PortError` and read full scale.
- New `disch` console command, `set_discharge()` in the library,
  `--disch` in the CLI; `params` reports the value.

## 3.1.0

- Bare-metal USB CDC-ACM console. One USB-C cable now carries power, console
  and firmware update; the debug probe is no longer needed. The UART console
  stays live in parallel as a fallback.
- Clocked from HSI48 trimmed by the CRS against USB SOF — no crystal on this
  board.
- Sample rate over the console rises from 12.8 Hz to 116 Hz measured.
- `usb_cdc_detach()` before the ROM bootloader jump, so DFU and the double-tap
  RESET escape hatch both keep working.
- Host tools select the board's own port by USB VID/PID instead of taking the
  first `usbmodem`.

## 3.0.0

- `stream`: continuous acquisition gated on the INTN pin, so every frame is a
  distinct conversion and polling can never silently re-read one.
- `mode g|f`: runtime grounded/floating switch (~30 ms) without re-uploading
  the PCAP04 firmware.
- Named parameter commands: `avrg`, `conv`, `refsel`, `comp`, `ports`.
- `params` and `health` decode the configuration and status registers.
- `C_AVRG` clamped to 256 — at 512 and above the reading goes 19.5 % low.

## 2.0.0

- ScioSense standard DSP firmware (548 bytes) and default configuration
  embedded and uploaded to the PCAP04's SRAM at start-up, since its NVRAM
  ships blank.
- First real capacitance measurements.

## 1.x

- Board bring-up: clocks, power sequencing, ADC, SPI, USB DFU flashing,
  diagnostic console, self-test.
