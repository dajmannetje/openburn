# Gweike G2 (BSL / SeaCAD galvo board, USB 04b4:1004) — protocol notes

> Decoded by Claude (Anthropic) from USB captures of GLaser V8.01.61, 2026-11-24.
> Used by `tools/g2bridge/g2bridge.py`, which lets OpenBurn drive the G2 as if it were GRBL.


Source: GLaser V8.01.61-20220712 traffic, captured on the laptop via usbmon while the
laser was shared to the PC over USB/IP. Files: ../captures/g2-session1.pcapng,
g2-session2.pcapng, timings + job settings in ../captures/marks.txt.
Extracted bulk transfers: s1.tsv / s2.tsv (epoch, devnum, endpoint, urb type, hex).

## Endpoints
- EP 0x06 OUT: control commands (one at a time)
- EP 0x88 IN : reply to each 0x06 command (strict request → reply)
- EP 0x02 OUT: job / path data blocks
- EP 0x84 IN : replies to data blocks

## Frame (all traffic except the very first 12-byte `01 23 00…`)
```
FE FF | LEN (u16 BE, = whole frame length) | F1 F2 | OP1 OP2 | payload … | CK
```
- CK (last byte) = (OP1 + OP2) & 0xFF — fits 29,354/29,395 control frames and
  2,248/2,260 data blocks (exceptions are board replies with other content).
- F1 F2: 80 00 / 81 00 / 80 01 / 81 01 / 01 00 / 01 01 … (direction / sequence flags,
  not decoded yet). Board replies use 81 01 / 80 01.

## Control commands seen (EP 0x06)
- `01 23 00 …` (12 bytes, unframed) — first thing GLaser sends (handshake/unlock?)
- `AA 05` — status poll (≈97 % of all commands). Reply 40 bytes, e.g.
  `feff0028 8101 aa05 0000 0100 … 0003 ff7f fffe …`
- `FF FB 01 xx` (xx = 01..06, 09, 10, 11) — read board parameter xx (calibration?)
- `FF F1`, `F0 F0` — at connect (F0F0 reply 60 bytes, looks like device ID/key)
- `AA 10`, `AA 18`, `AA 22`, `AA 2E`, `AA 34`, `AA C8`, `AA CA`, `01 71` — job control
  (start/stop/red light/…), to be mapped against preview vs mark timing.

## Data blocks (EP 0x02): header + 18-byte-ish records `02 OP LEN 00 …`, then padding
- `02 41` (len 18): path segment — coordinates u16 with field centre 0x8000; preview of
  the 10 mm square walks 0x8000 → 0x9555 in steps of 0x444
  (0x1555 = 5461 counts per 10 mm ≈ 546 counts/mm ≈ 120 mm over 65536).
- `02 45` (len 18): other segment type (mark vs jump?)
- `02 05`, `02 08`, `02 10`, `02 12`, `02 13`, `02 14`, `02 21`, `02 42`: settings
  records — candidates for power / speed / frequency / pulse width / delays.

## Config (GLaser BslCAD.cfg)
Work area 110 x 110 mm centred (WkX = WkY = -55). Galvo calibration (scale/bulge/skew/
trapezoid) is NOT in the file → probably stored on the board, read via FF FB 01 xx.

## Decoded 2026-11-24 (session timelines: s2-timeline.txt)
Job start (both preview and mark): AA 18 → AA 10 0001 → AA 10 0003 (status byte → 03 = armed).
Job end: final `02 41` jump to centre, AA 18, AA 10 0001 (status → 01 = idle). Status 04 = busy marking.
At connect GLaser also writes RAW (unframed) on EP02: `02 41 27 10 80 00 80 00 00 00 01 f4` (park centre) and sends `01 71`.

### Red-light preview
data: `02 21 0a 00 ff ff ff ff 00 00` (job start), `02 12 06 00 01 00` = RED LIGHT ON,
then outline as `02 41` records repeated (blocks of 21) until stop.

### Mark
data: `02 21 …ffffffff…`, `02 10 08 00 01 00 1f 40` = LASER OUTPUT ON (… 00 00 1f 40 = OFF at end),
`02 08 08 00 00 00 ff 38` laser-on delay = -200 (s16), `02 13 06 00 06 30` = 0x0630 = 1584 → 48 MHz/1584 ≈ 30.3 kHz (freq, probable),
`02 14 0a 00 PP PP 01 00 00 14` PPPP = power % × 65535 (20 % = 3333, 30 % = 4ccc, earlier circle 8ccc ≈ 55 %); 0x0014 = pulse width? (user: 10),
`02 42 12 00 00 01 00 31 …` (delays? end 50 / corner 80 / off 100 not mapped yet), mark segments `02 45`, end `02 05 04 00`.

### Segment records (02 41 jump / 02 45 mark), 18 bytes
`02 OP 12 00 | DUR u16 BE (µs) | 00 00 | X u24 BE | Y u24 BE | 00 00 00 00`
- centre 0x8000; ≈546 counts/mm (10 mm square side = 0x1555); GLaser splits lines into 2 mm pieces.
- DUR: 2 mm @1000 mm/s = 2000 µs (07d0), @500 mm/s = 4000 µs (0fa0) — confirmed by marks 1 vs 3.
- jumps (02 41) in preview: DUR field 0x029b / 0x2710 etc.

## Next
1. Diff mark1 / mark2 / mark3 settings records (power 20→30 %, speed 1000→500 mm/s).
2. Map AA xx commands to preview start/stop and mark start/stop (session2 has preview
   then mark3 back to back).
3. Decode 02 41 / 02 45 fields fully; verify mm→counts with the 10 mm square.
4. Read-only driver test on the laptop (connect + status), then red light (ASK USER FIRST).

## Orientation (red-light tests 2026-11-24, user standing in front of the laser)
- Second coordinate (u24 at record bytes 11..13, "Y" in my tools) NEGATIVE = LEFT  → this axis is the machine's left/right
  (matches LightBurn's "Galvo 2 is X axis" for the G2).
- First coordinate ("X" in my tools) POSITIVE = UP / away from the user (arrow test, 20 s).
- So for a design with u = right, v = up (as in OpenBurn / GRBL, mm from centre):
      galvo first coord  = v   (centre 0x8000 + v·65536/120)
      galvo second coord = u   (centre 0x8000 + u·65536/120)
  i.e. axes swapped, no mirroring.
- Long red-light runs: pace by clock (≤3 laps ahead) — sending faster made the board reset after ~20 s.

## Job completion (2026-11-24)
Data endpoint 0x02 is an in-order queue (park + laser-off queued after the job run after it).
Control commands on 0x06 (AA 10 0001 disarm) act IMMEDIATELY and abort queued work.
Status flips to 03 between job blocks → wait ≥ 1.5 × Σ(dur+delay) and 5× status 03 before disarming.
Multi-path jobs: 02 41 jump (dur = dist/4000 mm/s, delay 500) between paths inside the job — works.

## Frequency (02 13) — CONFIRMED 2026-11-24 (session 4, predicted 80 kHz correctly)
`02 13 06 00 VV VV`, VVVV = floor(1e6 / f_Hz) × 48   (period in whole µs, rounded DOWN, × 48 MHz ticks)
20 kHz 2400 | 30 kHz 1584 | 40 kHz 1200 | 60 kHz 768 | 80 kHz 576. Power/pulse record 02 14 unchanged by frequency.
- Pulse width: MOPA (10→20 ns) and Q (1→2 µs) changes do NOT change any record → not sent to this board; 02 14 tail '01 00 00 14' is constant.
