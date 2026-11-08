"""TF5 channel names and settings over Yamaha's remote control protocol (RCP).

The console listens on TCP 49280 and speaks LF-terminated text lines:

    -> get MIXER:Current/InCh/Label/Name 4 0
    <- OK get MIXER:Current/InCh/Label/Name 4 0 "Vox"
    <- NOTIFY set MIXER:Current/InCh/Fader/Level 4 0 -1250     (fader moved on the desk)
    <- NOTIFY set MIXER:Current/InCh/ToMix/Level 4 7 -600      (send to mix 8 changed)
    <- NOTIFY sscurrent_ex scene_a 12                         (scene recalled)
    -> prminfo 0
    <- OK prminfo 0 "MIXER:Current/InCh/Fader/Level" 40 0 -32768 1000 -32768 "dB" integer any rw 100

Addresses take two 0-based indices: x (the channel) and y (e.g. which mix a
send goes to). The protocol has no login, so ``tf5.username`` and
``tf5.password`` are accepted in the config but not sent.

What gets read for each patched source: name, colour, fader level, on, pan
(balance for ST) and, for input channels, the sends to the mixes, FX and mono;
plus the mix names. On connect the console is also asked for its parameter
list (``prminfo``); any input-channel gain / EQ / HPF / dynamics parameters it
lists are read as well. The TF5 this was built against lists none of those;
its EQ is read over the TF Editor protocol instead (``tf5.eq``, tfeditor.py).

USB capture channels carry whatever the TF's USB output patch routes to them;
RCP doesn't expose that patch, so ``tf5.usb_patch`` describes it (default: the
TF's recording patch, USB 1-32 = input channels 1-32, USB 33-34 = ST L/R).
"""

from __future__ import annotations

import logging
import re
from collections import deque
import shlex
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask import Blueprint, Flask, jsonify
from flask_sock import Sock

from .tfeditor import CURVE_FREQS, TFEditorClient, eq_curve

log = logging.getLogger(__name__)

DEFAULT_PORT = 49280
TF_INPUT_CHANNELS = 40
TF_MIXES = 20
TF_FX = 2
PRMINFO_PROBE = 512   # prminfo indices asked for on connect
NEG_INF = -32768      # fader/send level value meaning -inf dB
SEND_CHUNK = 200      # request lines per write, so a full poll doesn't flood the console

_PATCH_RE = re.compile(r"^\s*(?:(?:InCh|Ch)\s*(\d+)|St\s*([LR]))\s*$", re.IGNORECASE)
_PREFIX = {"InCh": "MIXER:Current/InCh/", "St": "MIXER:Current/St/", "Mix": "MIXER:Current/Mix/"}


@dataclass(frozen=True)
class Param:
    path: str            # part after the kind prefix, e.g. "Fader/Level"
    scale: int = 1       # raw / scale = value in unit
    unit: str = ""
    kind: str = "integer"  # integer | string
    count: int = 1       # y indices (e.g. 20 for per-mix sends)


NAME = Param("Label/Name", kind="string")
COLOR = Param("Label/Color", kind="string")
LEVEL = Param("Fader/Level", 100, "dB")
# Read for every patched source (and every mix). All are in the TF's parameter list.
BASE_PARAMS: dict[str, list[Param]] = {
    "InCh": [NAME, COLOR, LEVEL, Param("Fader/On"), Param("ToStereo/Pan"),
             Param("ToMix/Level", 100, "dB", count=TF_MIXES), Param("ToMix/On", count=TF_MIXES),
             Param("ToFx/Level", 100, "dB", count=TF_FX), Param("ToFx/On", count=TF_FX),
             Param("ToMono/Level", 100, "dB"), Param("ToMono/On")],
    "St": [NAME, COLOR, LEVEL, Param("Fader/On"), Param("Out/Balance")],
    "Mix": [NAME],
}
# Extra input-channel parameters taken from the console's prminfo list if present.
EXTRA_RE = re.compile(r"^(Port/HA/|HA/|PEQ|Eq|EQ|HPF|Hpf|LPF|Dyna|Gate|Comp|Att|Delay)")


def default_usb_patch() -> list[str]:
    return [f"InCh {n}" for n in range(1, 33)] + ["St L", "St R"]


def parse_patch_entry(entry: str) -> tuple[str, int] | None:
    """``"InCh 5"`` -> ("InCh", 4); ``"St R"`` -> ("St", 1); ``""`` -> None."""
    if not str(entry).strip():
        return None
    m = _PATCH_RE.match(str(entry))
    if not m:
        raise ValueError(f"unknown usb_patch entry {entry!r} (expected \"InCh N\", \"St L\", \"St R\" or \"\")")
    if m.group(1):
        n = int(m.group(1))
        if not 1 <= n <= TF_INPUT_CHANNELS:
            raise ValueError(f"usb_patch entry {entry!r}: input channel must be 1..{TF_INPUT_CHANNELS}")
        return "InCh", n - 1
    return "St", "LR".index(m.group(2).upper())


def patch_from(cfg: dict[str, Any]) -> list[tuple[str, int] | None]:
    """USB capture channel i -> the console source it carries."""
    entries = cfg["tf5"]["usb_patch"] or default_usb_patch()
    return [parse_patch_entry(e) for e in entries]


def source_label(src: tuple[str, int]) -> str:
    kind, x = src
    return f"InCh {x + 1}" if kind == "InCh" else f"St {'LR'[x]}"


def parse_line(line: str) -> tuple[str, str, list[str]] | None:
    """``OK get ADDR 4 0 "Vox"`` -> ("OK", "get", ["ADDR", "4", "0", "Vox"])."""
    try:
        parts = shlex.split(line, posix=True)
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    return parts[0], parts[1], parts[2:]


def _split_addr(addr: str) -> tuple[str, str] | None:
    for kind, prefix in _PREFIX.items():
        if addr.startswith(prefix):
            return kind, addr[len(prefix):]
    return None


def format_value(p: Param, raw: str | None) -> Any:
    if raw is None:
        return None
    if p.kind == "string":
        return raw
    try:
        v = int(raw)
    except ValueError:
        return raw
    if p.unit == "dB" and v <= NEG_INF:
        return None  # -inf
    return v / p.scale if p.scale != 1 else v


class TF5Client:
    """Keeps a connection to the console and the latest values for ``patch``.

    ``on_names`` gets {usb_index: name} every time a name changes; ``on_gains``
    gets {usb_index: fader dB or None (off / -inf)} every time a fader or on
    switch changes.
    """

    def __init__(self, host: str, port: int, patch: list[tuple[str, int] | None], poll_s: float,
                 on_names: Callable[[dict[int, str]], None],
                 on_gains: Callable[[dict[int, float | None]], None] | None = None) -> None:
        self.host, self.port, self.patch, self.poll_s = host, port, patch, poll_s
        self.on_names = on_names
        self.on_gains = on_gains
        self._gains_dirty = False
        self._lock = threading.Lock()
        # Every value change gets a sequence number, so browsers can be sent just
        # what changed (see changes_since); a status change is logged as None.
        self._changed = threading.Condition(self._lock)
        self._seq = 0
        self._changes: deque[tuple[int, tuple[str, int, str, int] | None]] = deque(maxlen=4096)
        self._params: dict[str, dict[str, Param]] = {k: {p.path: p for p in ps} for k, ps in BASE_PARAMS.items()}
        self._values: dict[tuple[str, int, str, int], str] = {}  # (kind, x, path, y) -> raw value
        self._discovered: list[str] = []                          # addresses from prminfo
        self._probe_left = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.connected = False
        self.last_error: str | None = None
        self.connects = 0
        self.updated: float | None = None

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tf5", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            base = {q.path for q in BASE_PARAMS["InCh"]}
            extras = sorted(p for p in self._params["InCh"] if p not in base)
            discovered = len(self._discovered)
        return {
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "connects": self.connects,
            "last_error": self.last_error,
            "updated": self.updated,
            "console_params": discovered,
            "extra_params": extras,
        }

    def _value(self, p: Param, kind: str, x: int) -> Any:
        if p.count == 1:
            return format_value(p, self._values.get((kind, x, p.path, 0)))
        return [format_value(p, self._values.get((kind, x, p.path, y))) for y in range(p.count)]

    def channels(self) -> list[dict[str, Any]]:
        """Per USB input: the console source it carries and that source's settings.

        Parameters with a y dimension (sends) give a list, one value per y.
        """
        out = []
        with self._lock:
            present = {key[:3] for key in self._values}  # (kind, x, path) with at least one value
            for i, src in enumerate(self.patch):
                entry: dict[str, Any] = {"index": i, "source": None, "params": {}}
                if src is not None:
                    kind, x = src
                    entry["source"] = source_label(src)
                    for path, p in self._params[kind].items():
                        if (kind, x, path) in present:
                            entry["params"][path] = {"value": self._value(p, kind, x), "unit": p.unit}
                out.append(entry)
        return out

    def mixes(self) -> list[str | None]:
        with self._lock:
            return [self._values.get(("Mix", m, NAME.path, 0)) for m in range(TF_MIXES)]

    def names(self) -> dict[int, str]:
        with self._lock:
            return {i: self._values[(src[0], src[1], NAME.path, 0)] for i, src in enumerate(self.patch)
                    if src is not None and (src[0], src[1], NAME.path, 0) in self._values}

    # ------------------------------------------------------------------ connection thread

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
                    log.warning("TF5 %s:%d: %s (retrying)", self.host, self.port, e)
                self.last_error = str(e)
            finally:
                if self.connected:
                    self.connected = False
                    self._note(None)
                self._sock = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(1.0)
            self.connected = True
            self._note(None)
            self.connects += 1
            self.last_error = None
            log.info("Connected to TF5 at %s:%d", self.host, self.port)
            self._probe_left = PRMINFO_PROBE
            self._send(sock, [f"prminfo {i}\n" for i in range(PRMINFO_PROBE)])
            buf = b""
            next_poll = 0.0
            last_rx = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_poll:
                    self._request_all(sock)
                    next_poll = now + self.poll_s
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    if time.monotonic() - last_rx > self.poll_s + 10:
                        raise TimeoutError("no reply from the console") from None
                    continue
                if not chunk:
                    raise ConnectionError("console closed the connection")
                last_rx = time.monotonic()
                buf += chunk
                *lines, buf = buf.split(b"\n")
                names_changed = False
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        names_changed |= self._handle(sock, line)
                if names_changed:
                    self._report()
                if self._gains_dirty:
                    self._gains_dirty = False
                    self._report_gains()

    def _send(self, sock: socket.socket, lines: list[str]) -> None:
        for i in range(0, len(lines), SEND_CHUNK):
            sock.sendall("".join(lines[i:i + SEND_CHUNK]).encode())
            if i + SEND_CHUNK < len(lines):
                time.sleep(0.02)

    def _request_all(self, sock: socket.socket) -> None:
        sources = sorted({s for s in self.patch if s is not None}) + [("Mix", m) for m in range(TF_MIXES)]
        with self._lock:
            reqs = [f"get {_PREFIX[kind]}{p.path} {x} {y}\n"
                    for kind, x in sources
                    for p in self._params[kind].values()
                    for y in range(p.count)]
        self._send(sock, reqs)

    def _handle(self, sock: socket.socket, line: str) -> bool:
        """Apply one reply line; True if a channel name changed."""
        parsed = parse_line(line)
        if parsed is None:
            return False
        status, action, args = parsed
        if action == "prminfo":
            self._on_prminfo(sock, status, args)
            return False
        if status == "NOTIFY" and action.startswith("sscurrent"):
            self._request_all(sock)  # scene recalled: everything may have changed
            return False
        if status not in ("OK", "NOTIFY") or action not in ("get", "set") or len(args) < 4:
            return False
        split = _split_addr(args[0])
        if split is None:
            return False
        kind, path = split
        try:
            x, y = int(args[1]), int(args[2])
        except ValueError:
            return False
        key = (kind, x, path, y)
        with self._lock:
            if path not in self._params[kind] or self._values.get(key) == args[3]:
                return False
            self._values[key] = args[3]
            self._log_change(key)
        self.updated = time.time()
        if path in ("Fader/Level", "Fader/On") and kind != "Mix":
            self._gains_dirty = True
        return path == NAME.path and kind != "Mix"

    def _on_prminfo(self, sock: socket.socket, status: str, args: list[str]) -> None:
        # OK prminfo <i> "<addr>" <x> <y> <min> <max> <default> "<unit>" <type> <ui> <rw> <scale>
        self._probe_left -= 1
        if status == "OK" and len(args) >= 12:
            addr, ycount, unit, typ = args[1], args[3], args[7], args[8]
            with self._lock:
                self._discovered.append(addr)
            split = _split_addr(addr)
            if split and split[0] == "InCh" and EXTRA_RE.match(split[1]) and ycount in ("0", "1"):
                try:
                    scale = max(1, int(args[11]))
                except ValueError:
                    scale = 1
                p = Param(split[1], scale, unit, "string" if typ in ("string", "binary") else "integer")
                with self._lock:
                    self._params["InCh"].setdefault(p.path, p)
        if self._probe_left == 0:
            with self._lock:
                n, extras = len(self._discovered), len(self._params["InCh"]) - len(BASE_PARAMS["InCh"])
            log.info("TF5 lists %d parameters; reading %d extra input-channel parameter(s)", n, extras)
            if extras:
                self._request_all(sock)

    # ------------------------------------------------------------------ change feed

    def _log_change(self, key: tuple[str, int, str, int] | None) -> None:
        """Record a change (caller holds the lock)."""
        self._seq += 1
        self._changes.append((self._seq, key))
        self._changed.notify_all()

    def _note(self, key: tuple[str, int, str, int] | None) -> None:
        with self._lock:
            self._log_change(key)

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        """(seq, full state): send first, then follow with changes_since(seq)."""
        with self._lock:
            seq = self._seq
        return seq, {"type": "tf5", "connected": self.connected, "channels": self.channels(), "mixes": self.mixes()}

    def changes_since(self, seq: int) -> tuple[int, dict[str, Any] | None]:
        """What changed after ``seq``: (new seq, message), message {} if nothing.

        The message is ``{"type": "tf5-changes", "connected", "changes": [{"i": usb input,
        "p": path, "y": index or null, "v": value, "u": unit}], "mixes": {mix: name}}``,
        with one entry per value (the latest). None means ``seq`` is too old to
        catch up from: send a fresh snapshot instead.
        """
        with self._lock:
            cur = self._seq
            if cur == seq:
                return seq, {}
            if not self._changes or self._changes[0][0] > seq + 1:
                return cur, None
            keys = {k for s, k in self._changes if s > seq and k is not None}
            changes: dict[tuple[int, str, int | None], dict[str, Any]] = {}
            mixes: dict[int, str] = {}
            for kind, x, path, y in keys:
                raw = self._values.get((kind, x, path, y))
                if kind == "Mix":
                    if path == NAME.path and raw is not None:
                        mixes[x] = raw
                    continue
                p = self._params[kind].get(path)
                if p is None:
                    continue
                for i, src in enumerate(self.patch):
                    if src == (kind, x):
                        yy = y if p.count > 1 else None
                        changes[(i, path, yy)] = {"i": i, "p": path, "y": yy, "v": format_value(p, raw), "u": p.unit}
        return cur, {"type": "tf5-changes", "connected": self.connected, "changes": list(changes.values()),
                     "mixes": mixes}

    def gains(self) -> dict[int, float | None]:
        """Per USB input whose channel state is known: fader dB, or None if off or at -inf."""
        out: dict[int, float | None] = {}
        with self._lock:
            for i, src in enumerate(self.patch):
                if src is None:
                    continue
                on = self._values.get((src[0], src[1], "Fader/On", 0))
                level = self._values.get((src[0], src[1], "Fader/Level", 0))
                if on is None or level is None:
                    continue
                out[i] = None if on == "0" else format_value(LEVEL, level)
        return out

    def _report_gains(self) -> None:
        if self.on_gains is None:
            return
        try:
            self.on_gains(self.gains())
        except Exception:
            log.exception("Applying TF5 fader levels failed")

    def _report(self) -> None:
        try:
            self.on_names(self.names())
        except Exception:
            log.exception("Applying TF5 channel names failed")


class TF5Module:
    """Reads the TF5's channel names and settings; feeds names to the audio engine.

    With ``tf5.eq`` it also reads every channel's EQ over the TF Editor protocol
    (tfeditor.py, read-only) for the spectrum overlay.

    Routes:
      GET /api/tf5/status     connection state, discovered parameters
      GET /api/tf5/channels   per USB input: console source and its settings; mix names
      GET /api/tf5/eq/<usb>   EQ of the console channel on that USB input + its curve
                              (404 when tf5.eq is off)
    """

    name = "tf5"

    def __init__(self, cfg: dict[str, Any], audio_engine: Any) -> None:
        t = cfg["tf5"]
        if t["username"] or t["password"]:
            log.info("tf5.username/password are set, but the TF5's remote protocol has no login; not sent")
        self.patch = patch = patch_from(cfg)
        self.client = TF5Client(str(t["host"]), int(t["port"]), patch, float(t["poll_s"]),
                                audio_engine.set_console_names, audio_engine.set_console_gains)
        # USB channels carrying the stereo bus are mixes, not sources (for ring attribution).
        audio_engine.set_bus_inputs([i for i, src in enumerate(patch) if src is not None and src[0] == "St"])
        self.eq_client: TFEditorClient | None = None
        self.eq_version = 0  # bumped whenever the console's mixing data (EQ included) changes
        if t["eq"]:
            self.eq_client = TFEditorClient(str(t["host"]), int(t["eq_port"]), on_change=self._eq_changed)
        # Open audio pages get desk changes pushed over their /ws/audio connection.
        audio_engine.console = self

    def _eq_changed(self) -> None:
        self.eq_version += 1

    @property
    def eq_available(self) -> bool:
        return self.eq_client is not None

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        return self.client.snapshot()

    def changes_since(self, seq: int) -> tuple[int, dict[str, Any] | None]:
        return self.client.changes_since(seq)

    def eq(self, usb: int) -> dict[str, Any]:
        """EQ of the console channel feeding USB input ``usb`` and its response curve (dB)."""
        ec = self.eq_client
        assert ec is not None
        src = self.patch[usb] if 0 <= usb < len(self.patch) else None
        settings = ec.eq(*src) if src is not None else None
        return {
            "index": usb,
            "source": source_label(src) if src is not None else None,
            "connected": ec.connected,
            "synced": ec.synced,
            "eq": settings,
            "freqs": [round(float(f), 2) for f in CURVE_FREQS] if settings else [],
            "curve": [round(float(v), 2) for v in eq_curve(settings)] if settings else [],
        }

    def register(self, app: Flask, sock: Sock) -> None:
        client = self.client
        bp = Blueprint("tf5", __name__, url_prefix="/api/tf5")

        @bp.get("/status")
        def status():
            return jsonify(self.status())

        @bp.get("/channels")
        def channels():
            return jsonify({"connected": client.connected, "channels": client.channels(),
                            "mixes": client.mixes()})

        @bp.get("/eq/<int:usb>")
        def eq(usb: int):
            if self.eq_client is None:
                return jsonify({"error": "tf5.eq is off"}), 404
            return jsonify(self.eq(usb))

        app.register_blueprint(bp)

    def start(self) -> None:
        self.client.start()
        if self.eq_client is not None:
            self.eq_client.start()

    def stop(self) -> None:
        self.client.stop()
        if self.eq_client is not None:
            self.eq_client.stop()

    def status(self) -> dict[str, Any]:
        st = self.client.status()
        st["eq"] = self.eq_client.status() if self.eq_client is not None else None
        return st
