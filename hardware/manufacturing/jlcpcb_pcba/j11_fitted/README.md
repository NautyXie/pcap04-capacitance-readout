# PCAP04 v1.5 - JLCPCB PCBA handoff (J11 fitted)

Files to upload separately:

1. Bare-board Gerber ZIP: ../../PCAP04_v1.5_100mm_C496550_JLCPCB_BARE_BOARD_UPLOAD.zip
2. BOM: PCAP04_v1.5_100mm_JLCPCB_PCBA_J11_BOM.csv
3. CPL: PCAP04_v1.5_100mm_JLCPCB_PCBA_J11_CPL.csv
4. Non-soldered accessory list: PCAP04_v1.5_J11_NON_SOLDERED_ACCESSORY.csv

Frozen scope:

- Fit 49 top-side SMT placements plus J11 through-hole header C49257.
- Do not fit J2-J7 SMA or J8-J10 headers.
- J11 is a wave-soldering part and may add a through-hole assembly fee.
- The C5305 jumper cap is a removable accessory; it is not soldered. Install it on J11 pins 1-2 for default USB 5 V. Ask JLC to install it as a manual operation, or fit it after delivery.
- U1 must be PCAP04-AQFM-24 / C2829318 with no substitution; add/pre-order it during the order.
- U5 must be TPS22918DBVR / C131941. Verified mapping: 1 VIN, 2 GND, 3 ON, 4 CT, 5 QOD, 6 VOUT.

Before payment:

- Confirm all 28 BOM rows match the exact LCSC numbers.
- Confirm J11 is matched to C49257 and listed for wave soldering.
- Add assembly note: install C5305 jumper cap on J11 pins 1-2; otherwise install it by hand after delivery.
- Confirm the JLC placement preview for U1-U7 pin-1, D1 cathode, and J1 orientation.
- Confirm C2829318 has been allocated to this order in My Parts Lib.
- Do not accept automatic substitutions.
