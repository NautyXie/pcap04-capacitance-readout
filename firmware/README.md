# Firmware

Bare-metal STM32G0B1CBT6 firmware. No vendor HAL, no CubeMX — every register
definition in `stm32g0b1.h` was written by hand against RM0444 Rev 6 and
DS13560 Rev 6.

Licence: **MIT**. `pcap04_stdfw.h` additionally carries ScioSense's MIT notice;
see the top of that file and `../LICENSE`.

## Build

```bash
make                # needs arm-none-eabi-gcc
```

~22 KB of the 128 KB flash. A prebuilt binary is in `prebuilt/`.

## Flash

```bash
cd ../host
python3 pcapdfu.py write ../firmware/prebuilt/pcap04_diag.bin
python3 pcapdfu.py run
```

See the top-level README for the one-time power-cycle caveat on a blank chip.

## Source map

| File | |
|---|---|
| `main.c` | clocks, GPIO, ADC, SPI, PCAP04 driver, console, commands |
| `usb_cdc.c/.h` | USB CDC-ACM device, polled, ~600 lines |
| `stm32g0b1.h` | register definitions, hand-verified against RM0444 |
| `board.h` | pin map, PCAP04 opcodes, board constants |
| `startup.c` | vector table and reset handler |
| `stm32g0b1cb.ld` | linker script; `.noinit` holds the double-tap magic word |
| `pcap04_stdfw.h` | ScioSense DSP firmware + default config (third-party, MIT) |

## Design notes

**Three console transports in parallel** — USB CDC, UART on J10, and an SWD
mailbox — and the main loop reads whichever speaks first. Keeping the UART alive
means a USB regression can never lock you out of the board.

**No interrupts except SysTick.** USB is polled from the main loop, so it cannot
perturb the SPI timing of a conversion in progress. Every hardware wait is
bounded with a per-peripheral timeout rather than a bare `while (!(REG & FLAG))`,
and `info` reports which ones ever fired.

**Escape hatch.** Double-tap RESET within 400 ms and the board enters the ROM
bootloader regardless of what the application firmware is doing. The magic word
lives in `.noinit`. `usb_cdc_detach()` runs first so the ROM's USB comes up on a
clean bus.

**Clocks.** HSI16 → PLL(M=1, N=8, R=÷2) → 64 MHz core, 2 flash wait states.
USB runs from HSI48 trimmed by the CRS against USB SOF packets — there is no
crystal on this board. Note `PLLR` encoding: `001` is ÷2, `000` is Reserved.

## Console commands

`help` lists them all. The ones you will actually use:

```
rail on|off          U5 load switch for the PCAP04 supply
load | loadf         upload the DSP firmware, grounded / floating
mode g|f             switch mode at runtime (~30 ms)
stream [n]           one line per conversion, gated on INTN
resd                 RES0..RES7 as Q5.27 ratios
avrg / conv / refsel / disch / comp / ports    front-end parameters
params               decode the current configuration
health               decode STATUS_0/1/2, INTN, rail voltage
usb                  CDC link state, bus resets, dropped bytes
dfu                  enter the ROM bootloader for reflashing
```
