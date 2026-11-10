"""Simulated TF5, the RCP side (console/rcp.py): answers ``prminfo`` / ``get`` with a
plausible service's channel list and pushes ``NOTIFY set`` when a fader moves "on the desk".
It also answers the meter commands (console/meters.py: ``mtrnum`` / ``mtrinfo`` /
``mtrstart`` / ``mtrstop``) in the grammar meters.py assumes, streaming ``NOTIFY mtr``
levels in dB x 100 that follow the demo desk's faders and ON switches.
"""

from __future__ import annotations

import logging
import math
import random
import socket
import threading
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)
RCP_PREFIX = {"InCh": "MIXER:Current/InCh/", "St": "MIXER:Current/St/", "Mix": "MIXER:Current/Mix/"}
# (kind, path, x count, y count, min, max, default, unit, type, scale): the shape of the
# TF's prminfo list for what console/rcp.py reads. Like the TF5, no EQ / gain addresses.
RCP_PARAMS = [
    ("InCh", "Label/Name", 40, 1, 0, 64, '"ch"', "", "string", 1),
    ("InCh", "Label/Color", 40, 1, 0, 8, '"Blue"', "", "string", 1),
    ("InCh", "Fader/Level", 40, 1, -32768, 1000, -32768, "dB", "integer", 100),
    ("InCh", "Fader/On", 40, 1, 0, 1, 1, "", "integer", 1),
    ("InCh", "ToStereo/Pan", 40, 1, -63, 63, 0, "", "integer", 1),
    ("InCh", "ToMix/Level", 40, 20, -32768, 1000, -32768, "dB", "integer", 100),
    ("InCh", "ToMix/On", 40, 20, 0, 1, 1, "", "integer", 1),
    ("InCh", "ToFx/Level", 40, 2, -32768, 1000, -32768, "dB", "integer", 100),
    ("InCh", "ToFx/On", 40, 2, 0, 1, 1, "", "integer", 1),
    ("InCh", "ToMono/Level", 40, 1, -32768, 1000, -32768, "dB", "integer", 100),
    ("InCh", "ToMono/On", 40, 1, 0, 1, 1, "", "integer", 1),
    ("St", "Label/Name", 2, 1, 0, 64, '"ST"', "", "string", 1),
    ("St", "Label/Color", 2, 1, 0, 8, '"White"', "", "string", 1),
    ("St", "Fader/Level", 2, 1, -32768, 1000, -32768, "dB", "integer", 100),
    ("St", "Fader/On", 2, 1, 0, 1, 1, "", "integer", 1),
    ("St", "Out/Balance", 2, 1, -63, 63, 0, "", "integer", 1),
    ("Mix", "Label/Name", 20, 1, 0, 64, '"Mix"', "", "string", 1),
]

# A Sunday service: (name, colour, fader dB x100 or None for -inf, on, pan).
DEMO_INPUTS = [
    ("Pastor", "Yellow", 0, 1, 0),
    ("Lead Vox", "Red", -350, 1, 0),
    ("BGV 1", "Pink", -800, 1, -20),
    ("BGV 2", "Pink", -800, 1, 20),
    ("Host", "Yellow", None, 0, 0),
    ("Kick", "Blue", -500, 1, 0),
    ("Snare", "Blue", -700, 1, 0),
    ("OH L", "Blue", -1200, 1, -63),
    ("OH R", "Blue", -1200, 1, 63),
    ("Bass DI", "Green", -400, 1, 0),
    ("Gtr", "Orange", -900, 1, -25),
    ("Keys L", "Purple", -1000, 1, -63),
    ("Keys R", "Purple", -1000, 1, 63),
    ("Acoustic", "Orange", -1100, 1, 25),
    ("Video L", "SkyBlue", None, 0, -63),
    ("Video R", "SkyBlue", None, 0, 63),
]
# The meters the simulated desk lists (mtrinfo): (kind, channels, points).
_IN_POINTS, _OUT_POINTS = ("PreHPF", "PreEQ", "PreFader", "PostOn"), ("PreEQ", "PreFader", "PostOn")
SIM_METERS = [
    ("InCh", 40, _IN_POINTS),
    ("StInCh", 4, _IN_POINTS),
    ("FxRtnCh", 4, _IN_POINTS),
    ("Mix", 20, _OUT_POINTS),
    ("Mtrx", 4, _OUT_POINTS),
    ("St", 2, _OUT_POINTS),
    ("Mono", 1, _OUT_POINTS),
]
METER_ADDRESSES = [(f"MIXER:Current/{k}/{p}", n) for k, n, points in SIM_METERS for p in points]
METER_FLOOR = -13800  # dB x 100 the simulated meters read for no signal

DEMO_MIXES = ["Wedge DS", "Wedge SR", "Drum IEM", "Vox IEM", "Keys IEM", "Gtr IEM", "Bass IEM", "Lobby"]


def demo_rcp_values() -> dict[tuple[str, int, int], str]:
    """(address, x, y) -> raw RCP token for the demo desk; anything absent is the default."""
    v: dict[tuple[str, int, int], str] = {}
    for kind, path, xs, ys, _, _, default, *_ in RCP_PARAMS:
        for x in range(xs):
            for y in range(ys):
                v[(RCP_PREFIX[kind] + path, x, y)] = str(default)
    inch = RCP_PREFIX["InCh"]
    for x in range(40):
        v[(inch + "Label/Name", x, 0)] = f'"ch {x + 1}"'
    for x, (name, color, level, on, pan) in enumerate(DEMO_INPUTS):
        v.update(
            {
                (inch + "Label/Name", x, 0): f'"{name}"',
                (inch + "Label/Color", x, 0): f'"{color}"',
                (inch + "Fader/Level", x, 0): str(-32768 if level is None else level),
                (inch + "Fader/On", x, 0): str(on),
                (inch + "ToStereo/Pan", x, 0): str(pan),
                (inch + "ToMono/Level", x, 0): "-1000",
            }
        )
        for m in range(len(DEMO_MIXES)):  # every source in every monitor mix, most turned down
            v[(inch + "ToMix/Level", x, m)] = str(-600 if (x + m) % 3 == 0 else -32768)
    v[(inch + "ToFx/Level", 1, 0)] = "-1200"  # reverb on the lead vocal
    for x, name in enumerate(["ST L", "ST R"]):
        v[(RCP_PREFIX["St"] + "Label/Name", x, 0)] = f'"{name}"'
        v[(RCP_PREFIX["St"] + "Fader/Level", x, 0)] = "0"
    for m in range(20):  # AUX 9-20 are stereo pairs, both halves named as on the desk ("Aux 9/10")
        pair = 9 + (m - 8) // 2 * 2
        v[(RCP_PREFIX["Mix"] + "Label/Name", m, 0)] = (
            f'"{DEMO_MIXES[m] if m < len(DEMO_MIXES) else f"Aux{pair:>2}/{pair + 1}"}"'
        )
    return v


class SimRCP:
    """Console side of TCP 49280: ``prminfo`` / ``get`` replies; ``NOTIFY set`` on desk changes.

    ``drift_s`` > 0 moves a random fader of the named inputs by a few dB every that
    many seconds, so open pages visibly follow "the desk". ``values`` (raw reply
    tokens by (address, x, y)) and ``prminfo`` (the parameter list's entries)
    default to the demo console's. ``received`` keeps the latest lines clients sent.
    """

    def __init__(
        self,
        values: dict[tuple[str, int, int], str] | None = None,
        *,
        prminfo: list[str] | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        drift_s: float = 0.0,
        seed: int = 1,
    ) -> None:
        self.values = demo_rcp_values() if values is None else values
        self.prminfo = (
            prminfo
            if prminfo is not None
            else [
                f'"{RCP_PREFIX[k]}{p}" {xs} {ys if ys > 1 else 0} {lo} {hi} {d} "{u}" {t} any rw {s}'
                for k, p, xs, ys, lo, hi, d, u, t, s in RCP_PARAMS
            ]
        )
        self.requests = 0
        self.received: deque[str] = deque(maxlen=100_000)
        self.on_set: Callable[[str, int, int, str], None] | None = None  # a change "on the desk"
        self.held: set[int] = set()  # input channels a client set the fader of: the drift leaves them
        self.meter_subs: dict[socket.socket, dict[str, list[float]]] = {}  # conn -> address -> [interval, due]
        self._meter_thread: threading.Thread | None = None
        self._wlock = threading.Lock()  # one writer at a time: replies, pushes, meter lines don't interleave
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.srv = socket.create_server((host, port))
        self.port = self.srv.getsockname()[1]
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._accept, name="sim-rcp", daemon=True).start()
        if drift_s > 0:
            threading.Thread(target=self._drift, args=(drift_s,), name="sim-rcp-drift", daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while chunk := conn.recv(65536):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                out = [self.reply(raw.decode(errors="replace").strip(), conn) for raw in lines]
                with self._wlock:
                    conn.sendall("".join(o + "\n" for o in out if o).encode())
        except OSError:
            pass
        finally:
            if conn in self.conns:
                self.conns.remove(conn)
            with self._lock:
                self.meter_subs.pop(conn, None)

    def reply(self, line: str, conn: socket.socket | None = None) -> str:
        """The console's answer to one line (``conn``: the client, for meters it starts)."""
        parts = line.split()
        if not parts:
            return ""
        self.requests += 1
        self.received.append(line)
        if parts[0].startswith("mtr"):
            return self._meter_reply(parts, conn)
        if parts[0] == "prminfo" and len(parts) == 2 and parts[1].isdigit():
            i = int(parts[1])
            return f"OK prminfo {i} {self.prminfo[i]}" if i < len(self.prminfo) else f"ERROR prminfo {i} UnknownIndex"
        if parts[0] == "get" and len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            with self._lock:
                value = self.values.get((parts[1], int(parts[2]), int(parts[3])))
            if value is None:
                return f"ERROR get {parts[1]} InvalidArgument"
            return f"OK get {parts[1]} {parts[2]} {parts[3]} {value}"
        return f"ERROR {parts[0]} UnknownCommand"  # tf-daemon never sends set; refuse anything else

    # ------------------------------------------------------------------ meters

    def _meter_reply(self, parts: list[str], conn: socket.socket | None) -> str:
        cmd, addrs = parts[0], dict(METER_ADDRESSES)
        if cmd == "mtrnum" and len(parts) == 1:
            return f"OK mtrnum {len(METER_ADDRESSES)}"
        if cmd == "mtrinfo" and len(parts) == 2 and parts[1].isdigit():
            i = int(parts[1])
            if i >= len(METER_ADDRESSES):
                return f"ERROR mtrinfo {i} UnknownIndex"
            addr, n = METER_ADDRESSES[i]
            return f'OK mtrinfo {i} "{addr}" {n} 0 {METER_FLOOR} 0 "dB" 100'
        if cmd == "mtrstart" and len(parts) == 3 and parts[1] in addrs and parts[2].isdigit():
            if conn is not None:
                with self._lock:
                    self.meter_subs.setdefault(conn, {})[parts[1]] = [max(20, int(parts[2])) / 1000, 0.0]
                if self._meter_thread is None:
                    self._meter_thread = threading.Thread(target=self._stream_meters, name="sim-rcp-mtr", daemon=True)
                    self._meter_thread.start()
            return f"OK mtrstart {parts[1]} {parts[2]}"
        if cmd == "mtrstop" and len(parts) == 2:
            with self._lock:
                if conn is not None:
                    self.meter_subs.get(conn, {}).pop(parts[1], None)
            return f"OK mtrstop {parts[1]}"
        return f"ERROR {cmd} InvalidArgument"

    def meter_levels(self, address: str, t: float) -> list[int]:
        """Plausible levels (dB x 100) for a meter at time ``t``: the named demo inputs talk and
        play around -20 dBFS; post-on meters follow the faders and ON switches; the stereo bus
        sums the inputs; everything else idles."""
        kind, point = address.split("/")[-2:]
        n = dict(METER_ADDRESSES)[address]
        if kind == "InCh":
            return [self._input_level(x, point, t) for x in range(n)]
        if kind == "St":
            power = sum(10 ** (self._input_level(x, "PostOn", t) / 1000) for x in range(len(DEMO_INPUTS)))
            bus = 10 * math.log10(power) - 3 if power > 0 else METER_FLOOR / 100
            return [self._clip(bus + self._fader_db("St", s) + 0.5 * s) for s in range(n)]
        return [self._clip(-40 + 4 * math.sin(0.5 * t + x)) for x in range(n)]

    def _input_level(self, x: int, point: str, t: float) -> int:
        if x >= len(DEMO_INPUTS):
            return METER_FLOOR
        db = (
            -20 + 5 * math.sin(0.7 * t + x) + 3 * math.sin(3.1 * t + 2 * x) + (1 if point in ("PreHPF", "PreEQ") else 0)
        )
        return self._clip(db + self._fader_db("InCh", x)) if point == "PostOn" else self._clip(db)

    def _fader_db(self, kind: str, x: int) -> float:
        with self._lock:
            on = self.values.get((RCP_PREFIX[kind] + "Fader/On", x, 0), "1")
            level = int(self.values.get((RCP_PREFIX[kind] + "Fader/Level", x, 0), "0"))
        return -1000.0 if on == "0" or level <= -32768 else level / 100

    @staticmethod
    def _clip(db: float) -> int:
        return METER_FLOOR if db <= METER_FLOOR / 100 else min(0, round(db * 100))

    def _stream_meters(self) -> None:
        while not self._stop.wait(0.02):
            now = time.monotonic()
            with self._lock:
                due = [(c, a, sub) for c, subs in self.meter_subs.items() for a, sub in subs.items() if now >= sub[1]]
            for c, a, sub in due:
                sub[1] = now + sub[0]
                self._write(c, f"NOTIFY mtr {a} " + " ".join(map(str, self.meter_levels(a, now))))

    def set(self, addr: str, x: int, y: int, value: str) -> None:
        """Change a value "on the desk": stored and pushed to every client (both protocols)."""
        self.store(addr, x, y, value)
        if self.on_set is not None:
            self.on_set(addr, x, y, value)

    def store(self, addr: str, x: int, y: int, value: str) -> None:
        """Store a value and push it to RCP clients (also for a change made over the other protocol)."""
        with self._lock:
            self.values[(addr, x, y)] = value
        self.push(f"NOTIFY set {addr} {x} {y} {value}")

    def push(self, line: str) -> None:
        for c in list(self.conns):
            self._write(c, line)

    def _write(self, conn: socket.socket, line: str) -> None:
        try:
            with self._wlock:
                conn.sendall((line + "\n").encode())
        except OSError:
            pass

    def _drift(self, period: float) -> None:
        addr = RCP_PREFIX["InCh"] + "Fader/Level"
        while not self._stop.wait(period):
            x = self._rng.randrange(len(DEMO_INPUTS))
            if x in self.held:
                continue
            with self._lock:
                level = int(self.values[(addr, x, 0)])
            if level > -32768:
                self.set(addr, x, 0, str(max(-4000, min(1000, level + self._rng.choice([-300, -150, 150, 300])))))

    def drop_connections(self) -> None:
        """Close every client connection, as a console reboot or cable pull would."""
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR)  # close() alone may not send a FIN while _serve is in recv
            except OSError:
                pass
            c.close()

    def close(self) -> None:
        self._stop.set()
        self.drop_connections()
        self.srv.close()


# ---------------------------------------------------------------------- both protocols
