"""TF5 level meters over RCP (TCP 49280): the SCP client's meter commands, read-only.

The TF Editor binary carries the commands ``mtrnum``, ``mtrinfo <i>``, ``mtrstart <address>
<interval>``, ``mtrstop <address>`` and the meter addresses ``MIXER:Current/<InCh | StInCh |
FxRtnCh | Mix | Mtrx | St | Mono>/<PreHPF | PreEQ | PreFader | PostOn>``
(docs/desk-coverage-howto-complete.md 3.1). How a TF5 answers them is UNVERIFIED, so the
parsing here assumes as little as it can:

    -> mtrnum
    <- OK mtrnum 25
    -> mtrinfo 0
    <- OK mtrinfo 0 "MIXER:Current/InCh/PreFader" 40 0 -13800 0 ...   (like prminfo: x, y, min, max)
    -> mtrstart MIXER:Current/InCh/PreFader 100                        (interval in ms)
    <- OK mtrstart MIXER:Current/InCh/PreFader 100
    <- NOTIFY mtr MIXER:Current/InCh/PreFader <type?> v0 v1 ... v39    (one value per channel)
    -> mtrstop MIXER:Current/InCh/PreFader

Words before the values (a type name) are skipped; values may be decimal or hex bytes (``0a``,
or one packed hex string); with more values than the meter has channels the last ones are
taken. A meter whose range (``mtrinfo`` min) or values go below zero is read as dB x 100
(≤ -13800 = -inf) and reported in dBFS; otherwise the values are raw meter steps and reported
as such (``scale: "raw"``) with the largest value they can take. When ``mtrnum`` isn't answered
the configured addresses are started anyway; a refused ``mtrstart`` is only reported.

The client keeps its own connection next to console/rcp.py's and sends nothing but those four
commands (``ALLOWED``): meters change no desk state.

Probe a real desk in one command (read-only; prints every reply line as it arrives):

    python -m tf_daemon.console.meters --host <console-ip> [--seconds 5] [--address ADDR ...]
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .patch import Source
from .rcp import DEFAULT_PORT, parse_line

log = logging.getLogger(__name__)

METER_KINDS = ("InCh", "StInCh", "FxRtnCh", "Mix", "Mtrx", "St", "Mono")
METER_POINTS = ("PreHPF", "PreEQ", "PreFader", "PostOn")
ALLOWED = re.compile(r"mtrnum|mtrinfo \d+|mtrstart \S+ \d+|mtrstop \S+")  # every line this sends
FLOOR_DB100 = -13800  # at or below (and RCP's -32768): no signal
MTRINFO_MAX = 256  # mtrinfo indices asked for at most
PROBE_S = 3.0  # wait this long for mtrnum / mtrinfo before starting meters anyway
SILENT_S = 5.0  # a started meter sending nothing this long is started again
_ADDR_RE = re.compile(rf"(?:^|/)({'|'.join(METER_KINDS)})/({'|'.join(METER_POINTS)})$")
_DEC = re.compile(r"-?\d+")
_HEX = re.compile(r"[0-9a-fA-F]+")


def meter_address(kind: str, point: str) -> str:
    """The address a meter has when the console doesn't list it (``mtrinfo``)."""
    return f"MIXER:Current/{kind}/{point}"


def meter_kind(address: str) -> tuple[str, str] | None:
    """``"MIXER:Current/InCh/PreFader"`` -> ("InCh", "PreFader"); None for anything else."""
    m = _ADDR_RE.search(address)
    return (m.group(1), m.group(2)) if m else None


@dataclass(frozen=True)
class MeterInfo:
    """One ``mtrinfo`` entry: the meter's address and, when given, channels and range."""

    index: int
    address: str
    channels: int | None = None
    lo: int | None = None
    hi: int | None = None
    fields: tuple[str, ...] = ()  # everything after the address, as sent


def parse_mtrinfo(args: list[str]) -> MeterInfo | None:
    """``["0", "MIXER:Current/InCh/PreFader", "40", "0", "-13800", "0"]`` -> MeterInfo."""
    if len(args) < 2 or not _DEC.fullmatch(args[0]):
        return None
    nums = [int(t) for t in args[2:] if _DEC.fullmatch(t)]
    return MeterInfo(
        int(args[0]),
        args[1],
        nums[0] if nums else None,
        nums[2] if len(nums) >= 4 else None,
        nums[3] if len(nums) >= 4 else None,
        tuple(args[2:]),
    )


def decode_values(tokens: list[str]) -> list[int] | None:
    """Meter values from the tokens after the address: decimal, hex bytes or one packed hex
    string; leading words (a type name) are skipped. None when they aren't numbers."""
    toks = list(tokens)
    while toks and not _HEX.fullmatch(toks[0]) and not _DEC.fullmatch(toks[0]):
        toks.pop(0)
    if not toks:
        return []
    if len(toks) == 1 and len(toks[0]) >= 8 and len(toks[0]) % 2 == 0 and _HEX.fullmatch(toks[0]):
        return [int(toks[0][i : i + 2], 16) for i in range(0, len(toks[0]), 2)]  # packed bytes
    if all(len(t) == 2 and _HEX.fullmatch(t) for t in toks) and any(
        t[0] == "0" or not t.isdigit() for t in toks
    ):  # "0a 7f 00": hex bytes (decimal isn't zero-padded)
        return [int(t, 16) for t in toks]
    if all(_DEC.fullmatch(t) for t in toks):
        return [int(t) for t in toks]
    return None


@dataclass
class Meter:
    """What is known about one meter address: its listing, start state and latest values."""

    address: str
    info: MeterInfo | None = None
    state: str = "listed"  # listed | starting | started | refused: <reply>
    values: list[int] = field(default_factory=list)
    updated: float = 0.0  # monotonic time of the latest values
    asked: float = 0.0  # monotonic time mtrstart was last sent
    is_db: bool = False  # values are dB x 100 (else raw meter steps)
    tokens: list[str] = field(default_factory=list)  # the latest data line's tokens, as sent

    def take(self, values: list[int], tokens: list[str]) -> None:
        n = self.info.channels if self.info and self.info.channels else None
        self.values = values[-n:] if n and len(values) > n else values
        self.tokens = tokens
        self.updated = time.monotonic()
        lo = self.info.lo if self.info else None
        self.is_db = self.is_db or (lo is not None and lo < 0) or any(v < 0 for v in values)
        if self.state != "started":
            self.state = "started"

    def level(self, x: int) -> float | int | None:
        """Channel ``x``'s level: dBFS (None = no signal) when dB, else the raw value."""
        if not 0 <= x < len(self.values):
            return None
        v = self.values[x]
        if self.is_db:
            return None if v <= FLOOR_DB100 else round(v / 100, 1)
        return v

    def raw_max(self) -> int:
        """The largest raw value: the listed maximum, else 127 or 255 by what was seen."""
        if self.info and self.info.hi and self.info.hi > 0:
            return self.info.hi
        top = max(self.values, default=0)
        return 127 if top <= 127 else 255 if top <= 255 else top


class MeterClient:
    """Keeps a meter connection to the console: discovers its meters, starts the ones the
    patch needs (input channels at ``point``, the stereo bus post-on) every ``interval_ms``,
    and keeps their latest values. ``since(version)`` gives the page what is new."""

    def __init__(
        self, host: str, port: int, patch: list[Source | None], interval_ms: int = 100, point: str = "PreFader"
    ) -> None:
        self.host, self.port, self.patch = host, port, patch
        self.interval_ms, self.point = interval_ms, point
        self.wanted = {k: point if k == "InCh" else "PostOn" for k in sorted({s[0] for s in patch if s})}
        self._lock = threading.Lock()
        self._meters: dict[str, Meter] = {}
        self._starting: deque[str] = deque()  # mtrstart sent, reply pending (replies come in order)
        self._listed: int | None = None  # mtrnum's answer; None = not answered
        self._probe_left = 0
        self._probe_until = 0.0
        self._started = False  # mtrstart sent in this session
        self.version = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.connected = False
        self.connects = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tf5-meters", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                self._send(sock, [f"mtrstop {a}" for a in self._addresses()])
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            meters = {
                a: {"state": m.state, "scale": "dB" if m.is_db else "raw", "channels": len(m.values)}
                for a, m in self._meters.items()
                if m.state != "listed"
            }
            listed = [m.address for m in self._meters.values() if m.info is not None]
        return {
            "connected": self.connected,
            "connects": self.connects,
            "last_error": self.last_error,
            "interval_ms": self.interval_ms,
            "point": self.point,
            "mtrnum": self._listed,
            "listed": listed,
            "meters": meters,
        }

    # ------------------------------------------------------------------ levels for the pages

    def levels(self) -> dict[str, Any]:
        """``{"type": "tf5-meters", "scale": "dB" | "raw" | None, "max", "levels": [per USB
        input: dBFS / raw value / None]}``. ``scale`` is the input channels' meter's; a USB
        input whose meter has another scale reads None."""
        with self._lock:
            by_kind = {k: self._meters.get(self._address(k, p)) for k, p in self.wanted.items()}
            main = by_kind.get("InCh") or next((m for m in by_kind.values() if m), None)
            live = main is not None and bool(main.values)
            scale = None if not live or main is None else "dB" if main.is_db else "raw"
            levels: list[float | int | None] = []
            for src in self.patch:
                m = by_kind.get(src[0]) if src else None
                if src is None or m is None or not m.values or m.is_db != (scale == "dB"):
                    levels.append(None)
                else:
                    levels.append(m.level(src[1]))
            top = main.raw_max() if live and main is not None and not main.is_db else None
        return {
            "type": "tf5-meters",
            "connected": self.connected,
            "point": self.point,
            "scale": scale,
            "max": top,
            "levels": levels,
        }

    def since(self, version: int) -> tuple[int, dict[str, Any] | None]:
        """(current version, levels) when there is news after ``version``, else (version, None)."""
        cur = self.version
        return (cur, None) if cur == version else (cur, self.levels())

    # ------------------------------------------------------------------ connection thread

    def _addresses(self) -> list[str]:
        with self._lock:
            return [a for a, m in self._meters.items() if m.state in ("starting", "started")]

    def _address(self, kind: str, point: str) -> str:
        """The console's address for a meter (as it listed it), else the default spelling."""
        for a, m in self._meters.items():
            if m.info is not None and meter_kind(a) == (kind, point):
                return a
        return meter_address(kind, point)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except OSError as e:
                if self._stop.is_set():
                    break
                if self.last_error != str(e):
                    log.warning("TF5 meters %s:%d: %s (retrying)", self.host, self.port, e)
                self.last_error = str(e)
            finally:
                if self.connected:
                    self.connected = False
                    self.version += 1
                self._sock = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(0.5)
            with self._lock:
                for m in self._meters.values():
                    m.state, m.values = "listed", []
                self._starting.clear()
            self._started, self._listed = False, None
            self.connected, self.last_error = True, None
            self.connects += 1
            self.version += 1
            self._probe_left, self._probe_until = 1, time.monotonic() + PROBE_S
            self._send(sock, ["mtrnum"])
            buf = b""
            while not self._stop.is_set():
                now = time.monotonic()
                if not self._started and (self._probe_left <= 0 or now >= self._probe_until):
                    self._start_meters(sock)
                elif self._started:
                    self._restart_silent(sock, now)
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    raise ConnectionError("console closed the connection")
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        self._handle(sock, line)

    def _send(self, sock: socket.socket, lines: list[str]) -> None:
        bad = [line for line in lines if not ALLOWED.fullmatch(line)]
        if bad:  # read-only: nothing but the meter commands ever leaves here
            raise ValueError(f"not a meter command: {bad[0]!r}")
        if lines:
            sock.sendall("".join(line + "\n" for line in lines).encode())

    def _start_meters(self, sock: socket.socket) -> None:
        self._started = True
        with self._lock:
            addrs = [self._address(k, p) for k, p in self.wanted.items()]
            for a in addrs:
                m = self._meters.setdefault(a, Meter(a))
                m.state, m.asked = "starting", time.monotonic()
                self._starting.append(a)
        self._send(sock, [f"mtrstart {a} {self.interval_ms}" for a in addrs])

    def _restart_silent(self, sock: socket.socket, now: float) -> None:
        with self._lock:
            silent = [
                m for m in self._meters.values() if m.state == "started" and now - max(m.updated, m.asked) > SILENT_S
            ]
            for m in silent:
                m.asked = now
        if silent:
            self._send(sock, [f"mtrstart {m.address} {self.interval_ms}" for m in silent])

    def _handle(self, sock: socket.socket, line: str) -> None:
        parsed = parse_line(line)
        if parsed is None:
            return
        status, action, args = parsed
        if action == "mtrnum":
            self._on_mtrnum(sock, status, args)
        elif action == "mtrinfo":
            self._probe_left -= 1
            info = parse_mtrinfo(args) if status == "OK" else None
            if info is not None:
                with self._lock:
                    self._meters.setdefault(info.address, Meter(info.address)).info = info
        elif action in ("mtrstart", "mtrstop") and status != "NOTIFY":
            if action == "mtrstart":
                self._on_mtrstart(status, args, line)
        elif action.startswith("mtr") and status in ("OK", "NOTIFY") and args:
            values = decode_values(args[1:])
            if values is None:
                return
            with self._lock:
                self._meters.setdefault(args[0], Meter(args[0])).take(values, args[1:])
            self.version += 1

    def _on_mtrnum(self, sock: socket.socket, status: str, args: list[str]) -> None:
        nums = [int(t) for t in args if _DEC.fullmatch(t)]
        if status != "OK" or not nums:
            log.info("TF5 doesn't answer mtrnum (%s); starting the meters anyway", " ".join(args) or status)
            self._probe_left = 0
            return
        self._listed = n = min(max(nums[-1], 0), MTRINFO_MAX)
        self._probe_left = n
        self._probe_until = time.monotonic() + PROBE_S
        self._send(sock, [f"mtrinfo {i}" for i in range(n)])

    def _on_mtrstart(self, status: str, args: list[str], line: str) -> None:
        with self._lock:
            addr = args[0] if args and args[0] in self._starting else (self._starting[0] if self._starting else None)
            if addr is None:
                return
            self._starting.remove(addr)
            m = self._meters[addr]
            if status == "OK":
                m.state = "started" if m.state == "starting" else m.state
            elif m.state == "starting":
                m.state = f"refused: {line}"
                log.info("TF5 refused meter %s: %s", addr, line)


# ---------------------------------------------------------------------- probe CLI


def probe(host: str, port: int, seconds: float, addresses: list[str], interval_ms: int, out: Any = print) -> None:
    """Print the console's meter list and a few seconds of meter data, every line as it is."""
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(0.2)
        buf = b""

        def send(line: str) -> None:
            assert ALLOWED.fullmatch(line), line
            out(f"-> {line}")
            sock.sendall((line + "\n").encode())

        def read(for_s: float, limit: int = 100_000) -> list[str]:
            nonlocal buf
            got: list[str] = []
            end = time.monotonic() + for_s
            while time.monotonic() < end and len(got) < limit:
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        out(f"<- {line}")
                        got.append(line)
            return got

        send("mtrnum")
        reply = read(1.5, limit=1)
        nums = [int(t) for t in (parse_line(reply[0]) or ("", "", []))[2] if _DEC.fullmatch(t)] if reply else []
        listed: list[str] = []
        for i in range(min(nums[-1], MTRINFO_MAX) if nums and reply[0].startswith("OK") else 0):
            send(f"mtrinfo {i}")
            for line in read(1.0, limit=1):
                p = parse_line(line)
                info = parse_mtrinfo(p[2]) if p and p[0] == "OK" else None
                if info is not None:
                    listed.append(info.address)
        if not addresses:
            addresses = [a for a in listed if meter_kind(a) in (("InCh", "PreFader"), ("St", "PostOn"))] or [
                meter_address("InCh", "PreFader"),
                meter_address("St", "PostOn"),
            ]
        for a in addresses:
            send(f"mtrstart {a} {interval_ms}")
        lines = read(seconds)
        for a in addresses:
            send(f"mtrstop {a}")
        read(0.3)  # the mtrstop replies
        data = [p for p in map(parse_line, lines) if p and p[1] not in ("mtrstart", "mtrstop")]
        out(f"-- {len(listed)} meter(s) listed, {len(data)} data line(s) in {seconds:g} s")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m tf_daemon.console.meters",
        description="Read-only probe of the TF5's RCP meters: mtrnum, mtrinfo, then a few seconds of mtrstart data.",
    )
    ap.add_argument("--host", required=True, help="console IP")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--seconds", type=float, default=3.0, help="how long to print meter data")
    ap.add_argument("--interval", type=int, default=100, help="mtrstart interval (ms)")
    ap.add_argument(
        "--address", action="append", default=[], help="meter address to start (repeatable; default InCh / St)"
    )
    args = ap.parse_args(argv)
    try:
        probe(args.host, args.port, args.seconds, args.address, args.interval)
    except OSError as e:
        print(f"{args.host}:{args.port}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
