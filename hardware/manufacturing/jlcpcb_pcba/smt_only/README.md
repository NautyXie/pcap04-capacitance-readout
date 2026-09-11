# PCAP04 v1.5 - JLCPCB SMT-only handoff

Files to upload separately:

1. Bare-board Gerber ZIP: ../../PCAP04_v1.5_100mm_C496550_JLCPCB_BARE_BOARD_UPLOAD.zip
2. BOM: PCAP04_v1.5_100mm_JLCPCB_SMT_ONLY_BOM.csv
3. CPL: PCAP04_v1.5_100mm_JLCPCB_SMT_ONLY_CPL.csv

Frozen scope:

- Fit 49 top-side SMT placements.
- Do not fit J2-J7 SMA or J8-J11 headers.
- The J11 jumper cap is a removable accessory; it is not soldered and is not in the BOM.
- U1 must be PCAP04-AQFM-24 / C2829318 with no substitution. Public JLC assembly stock was 0 when checked; pre-order/global-source it into My Parts Lib first.
- U5 must be TPS22918DBVR / C131941. Verified mapping: 1 VIN, 2 GND, 3 ON, 4 CT, 5 QOD, 6 VOUT.

Before payment:

- Confirm all 27 BOM rows match the exact LCSC numbers.
- Confirm the JLC placement preview for U1-U7 pin-1, D1 cathode, and J1 orientation.
- Confirm C2829318 has been allocated to this order in My Parts Lib.
- Do not accept automatic substitutions.
