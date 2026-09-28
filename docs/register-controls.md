# PCAP04 configuration editor / 全参数控制

The **前端参数** button now opens a grouped, scrollable editor for **90 user
configuration fields** from the ScioSense PCAP04 Rev.6 datasheet, §6.3.
The GUI, Python API, CLI and simulator share `host/pcap_registers.py`.

## Starting it / 启动

```sh
cd host
python3 pcap_gui.py --sim      # GUI demonstration, no serial hardware
python3 pcap_gui.py           # actual board, local serial port
python3 pcap_gui.py --port /dev/ttyACM0
```

A board attached to a Raspberry Pi needs a host able to access its serial port.
This change does not add an SSH serial transport. The GUI needs a graphical Tk
session; use the CLI over SSH for a headless Pi.

## Slower charging / 减慢充电

`RCHG_SEL=0` selects **10 kΩ**; `RCHG_SEL=1` selects **180 kΩ**.
`PRECHARGE_TIME` allocates time through that resistor; **1023 disables**
precharge. `FULLCHARGE_TIME` allocates the final direct charge; 1023 disables
that phase. Increasing a duration alone does not change the RC time constant.
The full-charge phase still has an unresisted edge. Allow sufficient settling
and verify the waveform/readout for the actual capacitance and cable.

No fixed timing value is recommended without checking the configured clock.
For cycle period `t_cycle` (datasheet §7.2.3.1–2):

| Phase | OLF | OHF |
| --- | --- | --- |
| Precharge, enabled | `(n+1)*t_cycle` | `(n+1)*t_cycle`, or `(n+2)*t_cycle` if full-charge disabled |
| Full charge, enabled | `(n+1)*t_cycle` | `(n+2)*t_cycle` |
| Discharge | `(n+1)*t_cycle` | `n*t_cycle` |

The datasheet is inconsistent about discharge code 1023 (Table 22 says off;
Table 67 gives a duration). It remains representable and is labelled with that
ambiguity; it is not a suggested operating value. The detailed CFG17 table is
used for FULLCHARGE_TIME high bits, despite the overview's DISCHARGE_TIME typo.
Guard attenuation has inconsistent units in Tables 29/70; the editor exposes
the code without inventing a physical unit.

## Workflow / 使用流程

1. Connect, power/load explicitly if needed, then open **前端参数**. Opening the
   editor pauses streaming and closes any current CSV recording.
2. Read values from hardware. No guessed defaults and no implicit firmware load.
3. Edit decimal or `0x` hexadecimal values. Apply writes **only changed fields**.
   A final configuration is validated before the first write. A field changed
   externally since the snapshot is rejected and must be refreshed.
4. RUNBIT is cleared, shared register bits are preserved, and every byte is
   read back. The complete configuration is verified before and after INIT/
   CDC_START. RUNBIT stays off if requested. On failure, the host attempts to
   clear RUNBIT and reports partial changes; it does not silently roll back or
   resume acquisition.
5. Start a new acquisition manually. The first two streamed samples after an
   editor write are discarded. Changing gain/reference topology/compensation
   clears the session's pF calibration; the saved calibration file is retained.
6. **导出JSON** saves the last readback snapshot, not unapplied edits.
   **导入JSON** fills fields only; it does not write until Apply is clicked.

The ordinary channel plot does not decode differential mode and does not
support every trigger configuration. The GUI prevents starting that plot with
RUNBIT off, PG5 INTN disabled, differential mode, or a noncontinuous trigger.
The controls remain available for these modes; use the raw console/results for
specialized acquisition. Changes to the OLF invalidate the old nominal 51 kHz
rate estimate. Measured rate remains the meaningful observation.

## CLI and Python / 命令行与接口

```sh
python3 pcap.py registers --list                # offline field catalog
python3 pcap.py -p /dev/ttyACM0 registers        # current decoded fields
# Example only: choose timings for the actual clock and sensor first.
python3 pcap.py -p /dev/ttyACM0 registers \
  --set RCHG_SEL=1 --set PRECHARGE_TIME=50
```

```python
from pcap04 import PCap04
with PCap04(port='/dev/ttyACM0') as dev:
    fields = dev.read_fields()
    # Assumes the user has explicitly powered and loaded the board.
    result = dev.write_fields({'RCHG_SEL': 1, 'PRECHARGE_TIME': 50},
                              expected={'RCHG_SEL': fields['RCHG_SEL'],
                                        'PRECHARGE_TIME': fields['PRECHARGE_TIME']})
    print(result['registers'])
```

`set_precharge(n)`, `set_fullcharge(n)` and `set_charge_resistor(code)` are
convenience wrappers. Callers must close an active stream before register I/O;
the GUI does that through its single serial worker. API callers should discard
initial conversions after a restart. A deliberately stopped/failed edit will
not be implicitly overwritten by `read()` or `stream()` in that connection;
explicit `load()` or a successful RUNBIT=1 edit is needed.

## Scope / 覆盖边界

Tabs cover charge/discharge, CDC, clocks/runtime, guard, RDC temperature,
DSP/GPIO, PWM/PDM, and firmware-defined algorithm fields. The full hardware
C_AVRG range 0..8191 is available, with the existing board-specific warning:
values above 256 are unvalidated and ≥512 previously read low. Legacy
`config --avrg` and speed presets retain their existing clamp.

Reserved, mandatory internal/TDC and factory CHARGE_PUMP bits are shown in the
raw snapshot but never edited. OX_AUTOSTOP_DIS, OX_STOP, RDCHG_OPEN and
HS_MODE_SEL are manufacturer internal controls, not user parameters.
MEM_LOCK, SERIAL_NUMBER and MEM_CTRL are special protection/nonvolatile
operations, excluded from this runtime editor. **No NVRAM store/erase is sent.**
RAM configuration disappears on power cycle or explicit load. Algorithm fields
marked linearize-only do not acquire that behavior with the bundled standard
DSP firmware. Guard requires correctly wired hardware; it is distinct from the
passive outer shield connected to PCB GND.

## Compatibility and verification

Uses existing MCU commands `cfg`, `wr`, `init`, `start`; no firmware binary
change or board flashing is needed. Verified with protocol fault-injection
unit tests and the native Tk simulator smoke test. This is **not hardware timing,
noise, or calibration validation**; no physical board was changed by development.
The simulator exercises configuration/readback, not the analog effects of each
advanced setting.

```sh
python3 -m unittest discover -s tests -v
python3 tests/gui_smoke.py   # needs working Tk/display, simulator only
```

Source: [ScioSense PCAP04 datasheet Rev.6, 2023-09-25](https://www.sciosense.com/wp-content/uploads/2023/12/PCAP04-Datasheet.pdf).
