# Measured results

Everything here was measured on one assembled v1.5 board. The scripts that
produced each number are in `host/characterisation/`. Where a figure disagrees
with the datasheet, the disagreement is stated rather than smoothed over.

Reference capacitor for all calibration: a 100 pF part fitted to `CTEST1`,
which bridges PC4/PC5 (floating channel 2). Its own tolerance sets the absolute
scale of everything below; the *shape* results (linearity, ratios) do not
depend on it.

---

## 1. On-chip reference capacitor

`range.py` / `calib.py --sweep`. The chip returns `ratio = C / Cref` in Q5.27,
so full scale is `32 × Cref` and the LSB is `Cref / 2²⁷`.

| C_REF_SEL | Cref measured | Full scale | LSB |
|---:|---:|---:|---:|
| 1 | 10.89 pF | 348 pF | 0.08 aF |
| 2 | 12.04 pF | 385 pF | 0.09 aF |
| 3 | 12.42 pF | 397 pF | 0.09 aF |
| 4 | 14.24 pF | 456 pF | 0.11 aF |
| 6 | 15.70 pF | 502 pF | 0.12 aF |
| 8 | 18.62 pF | 596 pF | 0.14 aF |
| 9 | 18.95 pF | 606 pF | 0.14 aF |
| 12 | 22.14 pF | 708 pF | 0.16 aF |
| 16 | 27.07 pF | 866 pF | 0.20 aF |
| 20 | 30.65 pF | 981 pF | 0.23 aF |
| 24 | 34.82 pF | 1114 pF | 0.26 aF |
| 28 | 38.33 pF | 1227 pF | 0.29 aF |
| 31 | 40.14 pF | **1285 pF** | 0.30 aF |

Fit: mean step **0.975 pF/code** (datasheet 0.959, +1.7 %), extrapolated
intercept **9.91 pF** (datasheet 3.23 pF, **+6.7 pF**).

The residual against an affine model is ±4 %, and the residual pattern is
*identical* between the compensation-on and compensation-off sweeps — so this
is the reference bank's real differential nonlinearity, not measurement noise.
The datasheet does say "Note: Step width varies."

**Consequence: calibrate at the code you use.** Do not interpolate, and do not
use the datasheet formula.

Going above 1285 pF requires an external reference capacitor (`C_REF_INT = 0`,
reference on PC0/PC1), which costs one floating channel.

## 2. External compensation = trace capacitance to ground

Measuring the same channel with `C_COMP_EXT` on and off, at ten different
reference settings:

```
ratio(off) / ratio(on) = 1.221370 ± 1.09e-4   (89 ppm)
```

Constant to 89 ppm while Cref varies 3.7:1. That simultaneously confirms the
`ratio = C/Cref` model and that compensation removes a *fixed* capacitance:

```
PC4/PC5 trace capacitance to ground = 22.14 pF
```

## 3. Electrode trace capacitance

Grounded mode, each electrode against ground, bare board:

```
PC0 12.81   PC1 13.94   PC2 17.45   PC3 13.78   PC4 132.36*  PC5 134.82*   pF
```

\* PC4/PC5 elevated by the 100 pF fitted on CTEST1 between them.

Fitting the bare electrodes against trace length gives

```
C = 4.18 pF + 0.158 pF/mm
```

Five of six electrodes fit within ±3 %. The one outlier, PC4 at −20 %, is
exactly the electrode the design review had flagged for being routed on L4
under the chassis pour instead of over the ground plane — the layout objection
was independently confirmed by measurement.

## 4. Noise

Referred to a 100 pF sensor. `avrg.py`.

| C_AVRG | CONV_TIME | rate | sd/mean | rms noise |
|---:|---:|---:|---:|---:|
| 32 | 2000 | 12.8 Hz | 194 ppm | 20.0 fF |
| 64 | 2000 | 12.8 Hz | 142 ppm | 14.7 fF |
| 128 | 2000 | 12.9 Hz | 62 ppm | 6.4 fF |
| 255 | 2000 | 6.9 Hz | 60 ppm | 6.2 fF |
| 256 | 8000 | 3.3 Hz | 38 ppm | 3.9 fF |

Roughly 1/√N, as expected.

The datasheet quotes 19 aF at 10 Hz (floating, fully compensated, 10 pF base).
This board is ~250× above that. Known contributors: the sensor is 10× larger,
the SPI bus is being polled *during* conversion (the datasheet explicitly warns
"Traffic on interface may enhance noise"), the precharge/discharge times are
ScioSense defaults tuned for ~10 pF, the reference (41 pF) is not matched to the
sensor (100 pF), and the part under test is an ordinary ceramic rather than a
C0G on an evaluation board.

### C_AVRG ceiling

| C_AVRG | reading |
|---:|---|
| ≤ 257 | ratio 2.4911 → 102.9 pF |
| ≥ 512 | ratio 2.0042 → 82.8 pF (**19.5 % low**) |

256 and 257 both read correctly, so this is not a zero-low-byte bug. Most
plausible cause is accumulator saturation with large capacitances. Firmware and
host both clamp at 256.

## 5. DISCHARGE_TIME

The ScioSense reference configuration ships `DISCHARGE_TIME = 0`. With a real
probe (~96 pF including cable) on PC2/PC3:

| DISCHARGE_TIME | rate | probe | 100 pF reference | status |
|---:|---:|---:|---:|---|
| 0 | 12.85 Hz | 1284.5 pF | 99.55 pF | `PortErr PC2,PC3` — pinned at full scale |
| 2 | 12.86 Hz | 96.61 pF | 99.73 pF | ok |
| 8 | 12.86 Hz | 96.59 pF | 99.97 pF | ok |
| 10 | 6.46 Hz | 96.31 pF | 99.99 pF | ok, but the conversion no longer fits one trigger period |

The datasheet lists the causes of `C_PortError` as: short to ground, discharge
resistivity too big, capacitance too big, or an ill-defined
precharge/fullcharge/discharge time. With a known-good 100 pF reading correctly
on the adjacent channel, and the fault persisting across every C_REF_SEL
setting, only the timing explanation survives.

Firmware ≥ 3.2.0 defaults to 8: clear margin, no rate cost.

## 6. Sample rate over USB

`rate.py`. Before firmware 3.1.0 the console was a UART behind a debug probe
and topped out near 12.8 Hz — a ~90-byte frame at 115200 baud plus the
request/response round trip. With the board's own USB CDC console the console
stops being the limit:

| CONV_TIME | nominal | achieved | ch2 |
|---:|---:|---:|---:|
| 2000 | 12.8 Hz | 12.9 Hz | 100.0085 pF |
| 800 | 31.9 Hz | 32.6 Hz | 100.0079 pF |
| 400 | 63.8 Hz | 64.4 Hz | 100.0072 pF |
| 200 | 127.5 Hz | 115.8 Hz | 100.0028 pF |

**9× improvement.** Above this the firmware's frame loop cannot keep up — each
frame does eight SPI result reads plus text formatting, and the period is 4 ms.

The reading holds at 100.007 ± 0.005 pF across a 9× rate change.

## 7. Power sequencing

- +3V3P rises to 3.297 V in **4 ms** after the U5 load switch is enabled
- discharge to below 50 mV takes **~43 ms** (RQOD1 470 Ω plus the switch's own
  25 Ω into 20 µF) — matching the design review's prediction
- the PCAP04's SRAM firmware does not survive this, so a reload is required
  after every power cycle

## 8. Watchdog

`WD_DIS` (cfg 0x1C) = 0x5A is effective: the chip holds `RUNBIT` for at least
50 s of complete SPI silence. A blank chip that has never been loaded, however,
triggers its own watchdog POR roughly every 10 s, and `POR_FLAG_WDOG` then
survives as a sticky flag into a subsequently healthy session — so treat that
flag as history, not as a live fault.
