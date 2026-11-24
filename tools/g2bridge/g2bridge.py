#!/usr/bin/env python3
"""g2bridge — make a Gweike G2 (BSL board, USB 04b4:1004) look like a GRBL laser.

OpenBurn (patched with a WebSocket transport) connects to ws://127.0.0.1:8767 and streams
ordinary GRBL G-code. The bridge collects the program until M2, then:
  * a program without any laser power (OpenBurn "Frame", all S0)  → red-light outline loop
  * a program with power                                           → G2 mark job(s)
Protocol details: ~/src/CtrlBSL/g2/DECODE.md (decoded from GLaser USB captures, 2026-11-24).

Safety
  * marking jobs are refused unless the same area was framed (red light) shortly before
    (--no-require-frame to disable);
  * points outside the galvo field are refused; --max-power caps the power;
  * a lone M3/M4 (OpenBurn's "fire" button) never fires — only complete programs run;
  * '!' (hold) and Ctrl-X (reset) abort: laser output OFF + disarm immediately;
  * the board's disarm (AA 10, immediate) is only sent after the queued job must have
    finished (status flips to 03 between blocks).

  g2bridge.py [--dry-run] [--port 8767] [--bed 110] [--freq 30] [--max-power 100]
              [--frame-seconds 10] [--no-require-frame]
"""
import argparse, base64, hashlib, math, socket, struct, sys, threading, time

# ------------------------------------------------------------------ G2 protocol
CPM = 65536 / 120.0            # galvo counts per mm (GLaser: full range = 120 mm)
CENTRE = 32768
JUMP_MM_S = 4000.0             # GLaser jump speed
JUMP_DELAY = 500               # µs after a jump (GLaser "jump position")
JUMP_DELAY_SHORT = 100         # µs after a short jump (< 0.5 mm, e.g. raster)
ON_DELAY = -200                # µs laser-on delay (user's GLaser setting)
OFF_DELAY = 100                # µs laser-off delay
CORNER_DELAY = 80              # µs at real corners (> 20°)
PIECE_MM = 2.0                 # GLaser splits lines into ≤ 2 mm pieces
MAX_RECS = 29                  # records per data block (530-byte frames)
PREVIEW_MM_S = 3000.0          # red-light speed

ARM = ["feff00148100aa180000000000000000000000c2", "feff00148100aa100001000000000000000000ba",
       "feff00148100aa100003000000000000000000ba"]
DISARM = ["feff00148100aa180000000000000000000000c2", "feff00148100aa100001000000000000000000ba"]
DISARM_JOB = "feff00148100aa100001000000000000000000ba"
STATUS = "feff00148100aa05001c000000000000000000af"
JOB_START = "02210a00ffffffff0000"
RED_ON = "021206000100"
LASER_ON = "0210080001001f40"
LASER_OFF = "0210080000001f40"
REC_42 = "024212000001003100000000000000000000"
REC_END = "02050400"


def frame(payload, ck=None):
    b = bytearray([0xfe, 0xff, 0, 0, 0x01, 0x00]) + payload + b"\x00"
    while len(b) < 19:
        b.append(0)
    b.append(ck if ck is not None else (payload[0] + payload[1]) & 0xff)
    b[2:4] = len(b).to_bytes(2, "big")
    return bytes(b)


def seg(op, dur, delay, x, y):
    """18-byte record. x = galvo 1 (up/away), y = galvo 2 (right), mm from centre."""
    cx, cy = round(CENTRE + x * CPM), round(CENTRE + y * CPM)
    assert 0 <= cx < 1 << 24 and 0 <= cy < 1 << 24
    return (bytes([0x02, op, 0x12, 0x00]) + int(min(max(round(dur), 0), 65535)).to_bytes(2, "big") +
            int(delay & 0xffff).to_bytes(2, "big") + cx.to_bytes(3, "big") + cy.to_bytes(3, "big") + b"\0\0\0\0")


def freq_value(khz):
    return int(1e6 / (khz * 1000)) * 48          # floor(period µs) × 48 — confirmed 20..80 kHz


def settings_frame(power_pct, khz):
    pw = round(max(0.0, min(100.0, power_pct)) / 100 * 65535)
    p = (bytes.fromhex("02080800") + b"\0\0" + (ON_DELAY & 0xffff).to_bytes(2, "big") +
         bytes.fromhex("02130600") + freq_value(khz).to_bytes(2, "big") +
         bytes.fromhex("02140a00") + pw.to_bytes(2, "big") + bytes.fromhex("01000014"))
    return frame(p, ck=0x1e)                      # GLaser's byte for this block (board ignores it)


def blocks_of(recs, even=False):
    if even:  # spread evenly (outline laps ending in a tiny block made the board reset)
        n = max(1, math.ceil(len(recs) / MAX_RECS))
        per = math.ceil(len(recs) / n)
    else:
        per = MAX_RECS
    out = []
    for i in range(0, len(recs), per):
        chunk = recs[i:i + per]
        out.append(frame(b"".join(chunk), ck=0x47 if any(r[1] == 0x45 for r in chunk) else None))
    return out


def rec_time_us(r):
    if r[1] in (0x41, 0x45):
        return int.from_bytes(r[4:6], "big") + int.from_bytes(r[6:8], "big")
    return 0

# ------------------------------------------------------------------ USB device


class G2:
    EP_CMD, EP_REPLY, EP_DATA, EP_DATA_REPLY = 0x06, 0x88, 0x02, 0x84

    def __init__(self, dry_run=False, log=print):
        self.dry = dry_run
        self.log = log
        self.dev = None
        self.lock = threading.RLock()

    def open(self):
        if self.dry:
            return True
        import usb.core, usb.util
        if self.dev is not None:
            return True
        dev = usb.core.find(idVendor=0x04b4, idProduct=0x1004)
        if dev is None:
            return False
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        for ep in (self.EP_CMD, self.EP_REPLY, self.EP_DATA, self.EP_DATA_REPLY):
            try:
                dev.clear_halt(ep)
            except Exception:
                pass
        self.dev = dev
        for ep, t in ((self.EP_REPLY, 100), (self.EP_DATA_REPLY, 50)):   # drain stale replies
            while True:
                try:
                    dev.read(ep, 512, timeout=t)
                except Exception:
                    break
        return True

    def close(self):
        if self.dev is not None:
            try:
                import usb.util
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None

    def send(self, ep, data):
        if self.dry:
            return b""
        with self.lock:
            try:
                self.dev.write(ep, data, timeout=1000)
                return bytes(self.dev.read(self.EP_REPLY if ep == self.EP_CMD else self.EP_DATA_REPLY, 512, timeout=500))
            except Exception as e:
                if "No such device" in str(e) or "Errno 19" in str(e):
                    self.dev = None
                    raise
                return b""

    def status(self):
        if self.dry:
            return 0x03
        try:
            r = self.send(self.EP_CMD, bytes.fromhex(STATUS))
            return r[10] if len(r) > 10 else 0xff
        except Exception:
            return 0xff

    def arm(self):
        for c in ARM:
            self.send(self.EP_CMD, bytes.fromhex(c))
        t = time.time()
        while time.time() - t < 3:
            if self.status() == 0x03:
                return True
        return False

    def safe_off(self):
        """Laser output off + disarm; never raises."""
        for ep, h in ((self.EP_DATA, frame(bytes.fromhex(LASER_OFF)).hex()), (self.EP_CMD, DISARM_JOB)):
            try:
                self.send(ep, bytes.fromhex(h))
            except Exception:
                pass

    # -------------------------------------------------- red-light outline loop
    def outline(self, paths, seconds, stop):
        recs = [seg(0x41, 0, 500, *paths[0][0])]
        prev = paths[0][0]
        for path in paths:
            if prev != path[0]:
                recs.append(seg(0x41, 1, 300, *path[0]))
            for a, b in zip(path, path[1:]):
                L = math.dist(a, b)
                if L < 1e-9:
                    continue
                n = max(1, math.ceil(L / PIECE_MM))
                for k in range(1, n + 1):
                    t = k / n
                    recs.append(seg(0x41, max(1, L / n / PREVIEW_MM_S * 1e6), 0,
                                    a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            prev = path[-1]
        if len(recs) > MAX_RECS and len(paths) == 1:   # one-block laps are the most reliable
            per = sum(math.dist(a, b) for a, b in zip(paths[0], paths[0][1:]))
            piece = max(PIECE_MM, per / (MAX_RECS - 2))
            recs = [seg(0x41, 0, 500, *paths[0][0])]
            for a, b in zip(paths[0], paths[0][1:]):
                L = math.dist(a, b)
                n = max(1, math.ceil(L / piece))
                for k in range(1, n + 1):
                    t = k / n
                    recs.append(seg(0x41, max(1, L / n / PREVIEW_MM_S * 1e6), 0,
                                    a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        blocks = blocks_of(recs, even=True)
        lap_us = sum(rec_time_us(r) for r in recs)
        self.log(f"frame: {len(recs)} records, {len(blocks)} block(s)/lap, lap {lap_us/1000:.1f} ms, {seconds:.0f} s")
        if self.dry:
            t = time.time()
            while time.time() - t < min(seconds, 2) and not stop.is_set():
                time.sleep(0.05)
            return True
        self.close()                         # fresh handle: the laser may have been power-cycled
        if not self.open():
            raise RuntimeError("G2 not connected — is the laser on and plugged into the laptop?")
        self.arm()
        self.send(self.EP_DATA, frame(bytes.fromhex(JOB_START)))
        self.send(self.EP_DATA, frame(bytes.fromhex(RED_ON)))
        start, sent = time.time(), 0.0
        try:
            while time.time() - start < seconds and not stop.is_set():
                drawn = (time.time() - start) * 1e6 / max(1, lap_us)
                if sent - drawn >= 3:
                    time.sleep(min(0.05, (sent - drawn - 2.5) * lap_us / 1e6))
                    continue
                for b in blocks:
                    self.send(self.EP_DATA, b)
                sent += 1
                self.status()
            self.send(self.EP_DATA, frame(seg(0x41, 30000, 500, 0, 0)))
            drawn = (time.time() - start) * 1e6 / max(1, lap_us)
            time.sleep(max(0, sent - drawn) * lap_us / 1e6 * 1.5 + 0.2)
        finally:
            for c in DISARM:
                try:
                    self.send(self.EP_CMD, bytes.fromhex(c))
                except Exception:
                    pass
        return True

    # -------------------------------------------------- marking job
    def mark(self, power, khz, recs, stop, progress=None):
        recs = recs + [bytes.fromhex(REC_42), bytes.fromhex(REC_END)]
        blocks = blocks_of(recs)
        job_us = sum(rec_time_us(r) for r in recs)
        self.log(f"job: power {power:.1f} %, {khz} kHz, {sum(1 for r in recs if r[1]==0x45)} mark pieces, "
                 f"{len(blocks)} blocks, est {job_us/1e6:.1f} s")
        if self.dry:
            t = time.time()
            while time.time() - t < min(job_us / 1e6, 2) and not stop.is_set():
                time.sleep(0.05)
            return not stop.is_set()
        self.close()                         # fresh handle: the laser may have been power-cycled
        if not self.open():
            raise RuntimeError("G2 not connected — is the laser on and plugged into the laptop?")
        ok = False
        try:
            if not self.arm():
                raise RuntimeError("board did not arm")
            self.send(self.EP_DATA, frame(bytes.fromhex(JOB_START)))
            self.send(self.EP_DATA, frame(bytes.fromhex(LASER_ON)))
            self.send(self.EP_DATA, settings_frame(power, khz))
            t0, queued = None, 0.0
            for i, b in enumerate(blocks):
                if stop.is_set():
                    raise RuntimeError("stopped")
                if t0 is not None:
                    while (queued / 1000 - (time.time() - t0) * 1000) > 600 and not stop.is_set():
                        time.sleep(0.005)
                self.send(self.EP_DATA, b)
                body = b[6:-1]
                j, us = 0, 0
                while j + 4 <= len(body) and body[j] == 0x02 and body[j + 2] >= 4:
                    us += rec_time_us(body[j:j + body[j + 2]])
                    j += body[j + 2]
                if t0 is None and us > 0:
                    t0 = time.time()
                queued += us
                if progress and i % 20 == 0:
                    progress(i / len(blocks))
            self.send(self.EP_DATA, frame(seg(0x41, 30000, 500, 0, 0)))     # park (queued)
            t0 = t0 or time.time()
            min_wait = job_us / 1e6 * 1.5 + 0.2
            limit = job_us / 1e6 * 4 + 10
            idle = 0
            while True:
                if stop.is_set():
                    raise RuntimeError("stopped")
                s = self.status()
                idle = idle + 1 if s == 0x03 else 0
                el = time.time() - t0
                if el >= min_wait and idle >= 5:
                    break
                if el > limit:
                    raise RuntimeError(f"job did not finish (status {s:02x})")
                time.sleep(0.02)
            ok = True
        finally:
            self.safe_off()
        return ok

# ------------------------------------------------------------------ G-code → moves


class Program:
    """Interprets the G-code subset OpenBurn emits (G0/G1/G2/G3, F, S, M3/M4/M5, M2, G90/G91, G21)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.x = self.y = 0.0
        self.abs = True
        self.feed = 1000.0          # mm/min
        self.s = 0.0
        self.laser = False
        self.moves = []             # (kind, (x0,y0), (x1,y1), s, feed_mm_min)
        self.warnings = set()
        self.lines = 0

    def feed_line(self, line):
        line = line.split(";")[0].split("(")[0].strip().upper()
        if not line:
            return
        self.lines += 1
        words = {}
        for tok in __import__("re").findall(r"([A-Z])\s*(-?\d*\.?\d+)", line):
            words.setdefault(tok[0], []).append(float(tok[1]))
        g = [int(v) if v == int(v) else v for v in words.get("G", [])]
        m = [int(v) for v in words.get("M", [])]
        if "F" in words:
            self.feed = words["F"][-1]
        if "S" in words:
            self.s = words["S"][-1]
        for code in m:
            if code in (3, 4):
                self.laser = True
            elif code == 5:
                self.laser = False
            elif code in (7, 8, 9):
                pass
        if 20 in g:
            raise ValueError("inch mode (G20) not supported")
        if 90 in g:
            self.abs = True
        if 91 in g:
            self.abs = False
        if 4 in g:
            self.warnings.add("G4 dwell ignored")
        motion = next((c for c in g if c in (0, 1, 2, 3)), None)
        if motion is None and ("X" in words or "Y" in words) and self.moves:
            motion = self._last_motion
        if "Z" in words:
            self.warnings.add("Z moves ignored")
        if motion is None or not ("X" in words or "Y" in words):
            return
        self._last_motion = motion
        nx = words["X"][-1] if "X" in words else (self.x if self.abs else 0)
        ny = words["Y"][-1] if "Y" in words else (self.y if self.abs else 0)
        if not self.abs:
            nx, ny = self.x + nx, self.y + ny
        p0 = (self.x, self.y)
        if motion in (2, 3):
            i = words.get("I", [0])[-1]; j = words.get("J", [0])[-1]
            cx, cy = self.x + i, self.y + j
            r = math.hypot(i, j)
            a0 = math.atan2(self.y - cy, self.x - cx); a1 = math.atan2(ny - cy, nx - cx)
            if motion == 2 and a1 >= a0: a1 -= 2 * math.pi
            if motion == 3 and a1 <= a0: a1 += 2 * math.pi
            n = max(2, int(abs(a1 - a0) * r / 0.2))
            pts = [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n + 1)]
            pts[-1] = (nx, ny)
        else:
            pts = [(nx, ny)]
        for q in pts:
            burn = motion != 0 and self.laser and self.s > 0
            self.moves.append(("mark" if burn else ("jump" if motion == 0 else "move"), p0, q, self.s, self.feed))
            p0 = q
        self.x, self.y = nx, ny

# ------------------------------------------------------------------ program → G2 jobs


class Translator:
    def __init__(self, bed, khz, max_power, max_s=1000.0, field=55.0, log=print):
        self.bed, self.khz, self.max_power, self.max_s, self.field, self.log = bed, khz, max_power, max_s, field, log

    def tool(self, p):
        """OpenBurn bed mm (origin front-left, Y away from you) → galvo (x = up/away, y = right)."""
        u, v = p[0] - self.bed / 2, p[1] - self.bed / 2
        return (v, u)

    def check(self, pts):
        for x, y in pts:
            if abs(x) > self.field or abs(y) > self.field:
                raise ValueError(f"design outside the laser field at X{y + self.bed/2:.1f} Y{x + self.bed/2:.1f} — "
                                 f"set OpenBurn's work area to {self.bed:.0f} x {self.bed:.0f} mm and keep the design inside it")

    def frame_paths(self, prog):
        paths, cur = [], []
        for kind, a, b, s, f in prog.moves:
            if kind == "move":
                if not cur:
                    cur = [self.tool(a)]
                cur.append(self.tool(b))
            elif cur:
                paths.append(cur); cur = []
        if cur:
            paths.append(cur)
        uniq, seen = [], set()
        for p in paths:
            key = tuple((round(x, 3), round(y, 3)) for x, y in p)
            if key not in seen:
                seen.add(key); uniq.append(p)
        for p in uniq:
            self.check(p)
        return uniq

    def jobs(self, prog):
        """List of (power %, records). Power changes split jobs, unless it is raster-like
        (many S changes) → one job at the highest S, lower S marked proportionally faster."""
        marks = [m for m in prog.moves if m[0] == "mark"]
        if not marks:
            return []
        changes = sum(1 for a, b in zip(marks, marks[1:]) if a[3] != b[3])
        groups = []
        if changes > 50:
            groups.append(prog.moves)
            self.log(f"raster-like program ({changes} power changes): one job, energy by speed")
        else:
            cur, cs = [], None
            for mv in prog.moves:
                if mv[0] == "mark" and cs is not None and mv[3] != cs and cur:
                    groups.append(cur); cur = []
                if mv[0] == "mark":
                    cs = mv[3]
                cur.append(mv)
            groups.append(cur)
        out = []
        for g in groups:
            gm = [m for m in g if m[0] == "mark"]
            if not gm:
                continue
            smax = max(m[3] for m in gm)
            power = smax / self.max_s * 100
            if power > self.max_power + 1e-6:
                raise ValueError(f"power {power:.0f} % above --max-power {self.max_power:.0f} %")
            out.append((power, self.records(g, smax)))
        return out

    def records(self, moves, smax):
        recs, pos = [], None
        marks = [i for i, m in enumerate(moves) if m[0] == "mark"]
        last_mark = marks[-1]
        for i, (kind, a, b, s, f) in enumerate(moves):
            if kind != "mark":
                continue
            ta, tb = self.tool(a), self.tool(b)
            self.check([ta, tb])
            if pos is None or math.dist(pos, ta) > 1e-6:
                d = 0 if pos is None else math.dist(pos, ta)
                recs.append(seg(0x41, d / JUMP_MM_S * 1e6, JUMP_DELAY if (pos is None or d > 0.5) else JUMP_DELAY_SHORT, *ta))
            v = max(1.0, f / 60.0) * (smax / max(s, 1e-9))        # mm/s, lower S → faster (same energy/mm)
            v = min(v, 4000.0)
            L = math.dist(ta, tb)
            n = max(1, math.ceil(L / PIECE_MM))
            nxt = moves[i + 1] if i + 1 < len(moves) else None
            chain_ends = nxt is None or nxt[0] != "mark" or i == last_mark
            corner = False
            if not chain_ends:
                nb = self.tool(nxt[2]); va = (tb[0] - ta[0], tb[1] - ta[1]); vb = (nb[0] - tb[0], nb[1] - tb[1])
                na, nbn = math.hypot(*va), math.hypot(*vb)
                if na > 1e-9 and nbn > 1e-9:
                    corner = (va[0] * vb[0] + va[1] * vb[1]) / (na * nbn) < math.cos(math.radians(20))
            for k in range(1, n + 1):
                t = k / n
                delay = 0 if k < n else (OFF_DELAY if chain_ends else (CORNER_DELAY if corner else 0))
                recs.append(seg(0x45, max(1, L / n / v * 1e6), delay, ta[0] + (tb[0] - ta[0]) * t, ta[1] + (tb[1] - ta[1]) * t))
            pos = tb
        return recs

# ------------------------------------------------------------------ minimal WebSocket server


class WS:
    def __init__(self, conn):
        self.c = conn
        self.wlock = threading.Lock()

    @staticmethod
    def handshake(conn):
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            req += chunk
        headers = {}
        for line in req.decode(errors="replace").split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            return None
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        return WS(conn)

    def _recvn(self, n):
        b = b""
        while len(b) < n:
            chunk = self.c.recv(n - len(b))
            if not chunk:
                raise ConnectionError("closed")
            b += chunk
        return b

    def recv(self):
        while True:
            h = self._recvn(2)
            op, ln = h[0] & 0x0f, h[1] & 0x7f
            if ln == 126: ln = struct.unpack(">H", self._recvn(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", self._recvn(8))[0]
            mask = self._recvn(4) if h[1] & 0x80 else b"\0\0\0\0"
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(self._recvn(ln)))
            if op == 8:
                raise ConnectionError("closed")
            if op == 9:
                self._send(0xA, data); continue
            if op in (1, 2, 0):
                return data.decode(errors="replace")

    def _send(self, op, data):
        hdr = bytes([0x80 | op])
        n = len(data)
        hdr += bytes([n]) if n < 126 else (bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        with self.wlock:
            self.c.sendall(hdr + data)

    def send(self, text):
        self._send(1, text.encode())

# ------------------------------------------------------------------ GRBL emulator


class Bridge:
    def __init__(self, a):
        self.a = a
        self.dev = G2(dry_run=a.dry_run, log=self.log)
        self.tr = Translator(a.bed, a.freq, a.max_power, log=self.log)
        self.prog = Program()
        self.state = "Idle"
        self.stop = threading.Event()
        self.worker = None
        self.last_frame = None          # (time, bbox in tool coords)
        self.ws = None

    def log(self, msg):
        print(time.strftime("%H:%M:%S"), msg, flush=True)

    def out(self, text):
        if self.ws:
            try:
                self.ws.send(text if text.endswith("\n") else text + "\r\n")
            except Exception:
                pass

    def status_line(self):
        x, y = self.prog.x, self.prog.y
        return f"<{self.state}|MPos:{x:.3f},{y:.3f},0.000|FS:0,0>\r\n"

    def realtime(self, ch):
        if ch == "?":
            self.out(self.status_line())
        elif ch in ("!", "\x18"):
            if self.worker and self.worker.is_alive():
                self.log("STOP requested → laser off + disarm")
                self.stop.set()
            if ch == "\x18":
                self.prog.reset()
                self.state = "Idle"
                self.out("\r\nGrbl 1.1h ['$' for help]\r\n")
        elif ch == "~":
            pass

    def line(self, ln):
        s = ln.strip()
        if not s:
            return
        up = s.upper()
        if up == "$I":
            self.out("[VER:1.1h.G2bridge:Gweike G2 (BSL)]\r\n[OPT:VL,15,128]\r\nok\r\n"); return
        if up == "$$":
            b = self.a.bed
            self.out(f"$30=1000\r\n$31=0\r\n$32=1\r\n$110=240000\r\n$111=240000\r\n$130={b}\r\n$131={b}\r\nok\r\n"); return
        if up == "$G":
            self.out("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\nok\r\n"); return
        if up.startswith("$"):
            self.out("ok\r\n"); return           # $X, $H, $J=… (jogging a galvo means nothing) etc.
        if self.worker and self.worker.is_alive():
            self.out("error:9\r\n"); return      # busy
        try:
            self.prog.feed_line(s)
        except ValueError as e:
            self.log(f"refused: {e}")
            self.out(f"[MSG:G2 bridge: {e}]\r\nerror:20\r\n")
            self.prog.reset(); return
        if up.split(";")[0].strip() in ("M2", "M30"):
            prog, self.prog = self.prog, Program()
            self.prog.x, self.prog.y = prog.x, prog.y
            self.stop.clear()
            self.worker = threading.Thread(target=self.run_program, args=(prog,), daemon=True)
            self.worker.start()                 # final 'ok' is sent when the laser is done
            return
        self.out("ok\r\n")

    def run_program(self, prog):
        self.state = "Run"
        try:
            for w in sorted(prog.warnings):
                self.log(f"warning: {w}")
            jobs = self.tr.jobs(prog)
            if not jobs:
                paths = self.tr.frame_paths(prog)
                if not paths:
                    self.out("ok\r\n"); return
                pts = [p for path in paths for p in path]
                bbox = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
                self.out("[MSG:G2 bridge: red-light frame]\r\n")
                self.dev.outline(paths, self.a.frame_seconds, self.stop)
                if not self.stop.is_set():
                    self.last_frame = (time.time(), bbox)
                self.out("ok\r\n")
                return
            pts = []
            for _, recs in jobs:
                for r in recs:
                    if r[1] == 0x45:
                        pts.append(((int.from_bytes(r[8:11], "big") - CENTRE) / CPM, (int.from_bytes(r[11:14], "big") - CENTRE) / CPM))
            bbox = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
            if self.a.require_frame:
                lf = self.last_frame
                ok = lf and time.time() - lf[0] < 600 and all(
                    [bbox[0] >= lf[1][0] - 0.6, bbox[1] >= lf[1][1] - 0.6, bbox[2] <= lf[1][2] + 0.6, bbox[3] <= lf[1][3] + 0.6])
                if not ok:
                    self.log("refused: job not framed first")
                    self.out("[MSG:G2 bridge: press Frame first (red light), then Start]\r\nerror:9\r\n")
                    return
            for n, (power, recs) in enumerate(jobs, 1):
                if self.stop.is_set():
                    break
                self.out(f"[MSG:G2 bridge: marking {n}/{len(jobs)} at {power:.0f} %]\r\n")
                self.dev.mark(power, self.a.freq, recs, self.stop)
            if self.stop.is_set():
                self.log("job stopped")
                return
            self.out("ok\r\n")
        except Exception as e:
            self.log(f"ERROR: {e}")
            self.dev.safe_off() if not self.a.dry_run else None
            self.out(f"[MSG:G2 bridge: {e}]\r\nerror:20\r\n")
        finally:
            self.state = "Idle"

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.a.port))      # localhost only
        srv.listen(1)
        self.log(f"g2bridge listening on ws://127.0.0.1:{self.a.port}  ({'DRY RUN' if self.a.dry_run else 'laser live'}, "
                 f"bed {self.a.bed} mm, {self.a.freq} kHz, max power {self.a.max_power} %, "
                 f"{'frame required' if self.a.require_frame else 'no frame check'})")
        while True:
            conn, addr = srv.accept()
            ws = WS.handshake(conn)
            if not ws:
                conn.close(); continue
            self.ws = ws
            self.log(f"OpenBurn connected from {addr[0]}")
            self.out("\r\nGrbl 1.1h ['$' for help]\r\n")
            buf = ""
            try:
                while True:
                    for ch in ws.recv():
                        try:
                            if ch in "?!~\x18":
                                self.realtime(ch)
                            elif ch in "\r\n":
                                if buf:
                                    self.line(buf)
                                buf = ""
                            else:
                                buf += ch
                        except Exception as e:
                            import traceback
                            self.log(f"internal error on {buf!r}: {e}\n{traceback.format_exc()}")
                            self.out("error:1\r\n")
                            buf = ""
            except Exception as e:
                self.log(f"OpenBurn disconnected ({e})")
                if self.worker and self.worker.is_alive():
                    self.stop.set()
                self.ws = None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--bed", type=float, default=110.0, help="OpenBurn bed size (square, mm); centre = galvo centre")
    ap.add_argument("--freq", type=float, default=30.0, help="kHz")
    ap.add_argument("--max-power", type=float, default=100.0)
    ap.add_argument("--frame-seconds", type=float, default=10.0)
    ap.add_argument("--no-require-frame", dest="require_frame", action="store_false")
    Bridge(ap.parse_args()).serve()


if __name__ == "__main__":
    main()
