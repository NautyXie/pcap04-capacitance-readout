# Characterisation scripts

These reproduce the numbers in `../../docs/measurements.md`. They are
measurement sweeps rather than polished tools — read them before running them,
since several deliberately push the chip outside its default configuration.

| Script | What it measures |
|---|---|
| `calib.py` | Cref against a known capacitor across all C_REF_SEL codes; also the external-compensation offset |
| `range.py` | full scale and LSB per reference setting |
| `avrg.py` | noise versus averaging depth, and the C_AVRG >= 512 anomaly |
| `disch.py` | the DISCHARGE_TIME threshold for a real sensor |
| `rate.py` | achieved sample rate versus the chip's trigger period |
| `session.py` | minimal prompt-framed console session, used by the others |

Each expects a board with a known capacitor fitted. `calib.py` and `range.py`
assume 100 pF on channel 2 (`CTEST1`); change `KNOWN`/`CNOM` at the top if
yours differs.
