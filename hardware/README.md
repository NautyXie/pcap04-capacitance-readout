# Hardware

KiCad 7 project for the PCAP04 3-channel capacitance readout board.
100 × 100 mm, 4 layers, SMA edge connectors.

Licence: **CERN-OHL-P-2.0** (permissive). See `../LICENSES/CERN-OHL-P-2.0.txt`.

## Files

```
PCAP04_Acquisition_3CH_v1.5_100mm_C496550.kicad_pro   project
                                          .kicad_sch   root sheet
                                          .kicad_pcb   layout
                                          .kicad_dru   design rules
MCU_CONTROL.kicad_sch      STM32G0B1, SWD, USB, console header
USB_POWER.kicad_sch        USB-C input, protection, rails, U5 load switch
PCAP04_Acquisition.lib     custom symbols
PCAP04_Custom.pretty/      custom footprints
3dmodels/                  3D models for the custom parts
manufacturing/             gerbers, drill, assembly data
```

## Getting one made

`manufacturing/` is ready to upload as-is. It was produced for and verified at
JLCPCB, but the gerbers are standard.

- 4 layers, 1.6 mm, HASL or ENIG both fine
- the SMA edge connectors are C496550 (LCSC part number)

## Layer stack and why it matters here

| Layer | Use |
|---|---|
| L1 F.Cu | components, sense lines |
| L2 In1.Cu | ground plane |
| L3 In2.Cu | power and signal |
| L4 B.Cu | chassis pour |

The sense traces to the SMA connectors want a continuous ground plane directly
underneath. Measured trace capacitance follows

```
C = 4.18 pF + 0.158 pF/mm
```

for five of the six electrodes, within ±3 %. **PC4 is 20 % off** because it is
routed on L4 under the chassis pour rather than over the ground plane. That was
flagged in the design review before assembly and then confirmed by measurement
— see `../docs/measurements.md` §3. Fix it if you respin.

## Known limitations of v1.5

**`PCAUX` is not routed.** That pin is the PCAP04's integrated guard driver
output. Without it, guarding (`C_G_EN`) cannot be used, which matters if you
want long cables to remote electrodes.

**`PT0REF` / `PT1` / `PTOUT` are not routed.** The resistance-to-digital
converter therefore has no external reference resistor, so external Pt
temperature measurement is unavailable. `RES6`/`RES7` still return numbers, but
treat them only as drift indicators.

**RPS1 was assembled as 33 kΩ where the BOM says 330 kΩ.** This is an assembly
deviation on the built board, not a design error — it changes the +3V3P sense
divider to 133/100. The firmware compensates (`board.h`, `RAIL_DIV_NUM`), and
the `div` console command overrides it. Check what is actually fitted on yours.

If you respin, routing `PCAUX` and the PT pins out to test points would be the
two highest-value changes.
