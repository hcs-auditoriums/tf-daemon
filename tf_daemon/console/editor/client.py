"""TFEditorClient: one TF Editor protocol session with the console (see __init__)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from .codec import (
    CLIENT_TYPE,
    DESC_MD5,
    DUMP,
    EVENT,
    EVENT_REPLY,
    EVENT_TAG,
    HASH_COMPARE,
    ORIGIN_SELF,
    SET_PARAM,
    T_S32,
    T_STR,
    T_U8,
    T_U16,
    T_U32,
    TAGS,
    Element,
    FrameReader,
    Message,
    ProtocolError,
    event,
    frame,
    hash_compare,
    parse_message,
    set_param_message,
    subscribe,
    sync,
)
from .dump import Dump, channel_eq, dump_blob
from .guard import ALLOWED_EVENT_REPLIES, FADER_WRITABLE, WRITABLE, ReadOnlyViolation, check_outbound, write_limits

log = logging.getLogger(__name__)

DEFAULT_PORT = 50368
MAX_FRAME = 4 * 1024 * 1024  # the Mixing dump is 67 kB; anything near this is garbage
KEEPALIVE_S = 10.0  # our keepalive period (the guide: every <= 15 s)
RX_TIMEOUT_S = 35.0  # no frame from the console for this long: reconnect
SYNC_TIMEOUT_S = 20.0  # connect -> Mixing dump
CLIENT_TYPE_WAIT_S = 3.0  # SetClientType acked but no onSetClientType: go on after this
WRITE_CONFIRM_S = 3.0  # write(): how long to wait for the console to echo each change


class TFEditorClient:
    """Keeps a TF Editor protocol session; holds the console's Mixing data.

    ``on_change`` (optional) is called with no arguments after the Mixing data
    changed (dump received or a parameter pushed). With ``writes`` False (the
    default) the client is read-only; with True, ``write()`` may change EQ
    parameters when a person asks for it, and with ``fader_writes`` as well, input
    channels' fader level and on switch.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        on_change: Callable[[], None] | None = None,
        writes: bool = False,
        fader_writes: bool = False,
    ) -> None:
        self.host, self.port = host, port
        self.writes = writes
        self.fader_writes = writes and fader_writes
        self._write_lock = threading.Lock()  # one write() at a time
        self._authorized: dict[tuple[tuple[int, ...], tuple[int, ...]], tuple[int, int]] = {}  # addr -> (type, value)
        self._echo = threading.Condition()
        self._awaiting: dict[tuple[tuple[int, ...], tuple[int, ...]], int | str | None] = {}  # addr -> echoed value
        self.on_change = on_change
        self._lock = threading.Lock()
        self._mixing: Dump | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.connected = False
        self.synced = False
        self.connects = 0
        self.last_error: str | None = None
        self.updated: float | None = None
        self.changes = 0  # parameter pushes applied
        self.rejected = 0  # Mixing pushes that didn't fit our copy of the data (should stay 0)
        self.last_rejected: str | None = None
        self.received: dict[str, int] = {}  # "TAG cc dd ee" -> frames received (diagnostics)
        self.desc_md5: str | None = None
        self.sent: list[tuple[str, tuple[int, int, int]]] = []  # (tag, cmd) of the last frames sent
        self._phase = "idle"
        self._mixing_due: float | None = None

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="tfeditor", daemon=True)
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
        return {
            "host": self.host,
            "port": self.port,
            "connected": self.connected,
            "synced": self.synced,
            "connects": self.connects,
            "last_error": self.last_error,
            "updated": self.updated,
            "changes": self.changes,
            "rejected": self.rejected,
            "last_rejected": self.last_rejected,
            "received": dict(self.received),
            "descriptor_md5": self.desc_md5,
            "descriptor_known": self.desc_md5 in (None, DESC_MD5["Mixing"]),
            "read_only": not self.writes,
            "fader_writes": self.fader_writes,
        }

    def eq(self, kind: str, x: int) -> dict[str, Any] | None:
        with self._lock:
            return channel_eq(self._mixing, kind, x) if self._mixing is not None else None

    def get(self, path: str) -> int | str | None:
        """Any Mixing parameter by name path, e.g. ``"InputChannel:0/Label/Name"``."""
        with self._lock:
            if self._mixing is None:
                return None
            try:
                return self._mixing.get(path)
            except KeyError:
                return None

    def write(self, changes: list[tuple[str, int]], timeout: float = WRITE_CONFIRM_S) -> list[dict[str, Any]]:
        """CHANGE PARAMETERS ON THE CONSOLE: ``[(name path, raw value), ...]``.

        Only for an explicit request by a person; nothing calls this on its own. Only
        WRITABLE paths, values within WRITE_LIMITS, and only with ``writes`` enabled;
        anything else raises before a byte is sent. Returns per change whether the
        console echoed it back within ``timeout`` and the value it reported. A change to the
        value the console already has isn't sent (the TF5 doesn't echo a set that changes
        nothing, so it could never be confirmed); it's reported as confirmed.
        """
        if not self.writes:
            raise PermissionError("writing to the console is off (tf5.eq_write = false)")
        sock = self._sock
        plan: list[tuple[str, tuple[tuple[int, ...], tuple[int, ...]], Any, int]] = []
        already: dict[str, int] = {}  # path -> value the console has already
        with self._lock:
            dump = self._mixing
            if not self.synced or sock is None or dump is None:
                raise ConnectionError("not connected to the console's editor protocol")
            for path, value in changes:
                if not (WRITABLE.fullmatch(path) or (self.fader_writes and FADER_WRITABLE.fullmatch(path))):
                    raise ValueError(f"{path}: not a writable EQ parameter")
                lo, hi = write_limits(path)
                if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                    raise ValueError(f"{path}: {value!r} outside {lo}..{hi}")
                try:
                    wp, idx, prm = dump.address(path)
                except KeyError:
                    raise ValueError(f"{path}: not on this console") from None
                if prm.type not in (1, 2) or (prm.type == 2 and value < 0):
                    raise ValueError(f"{path}: not a numeric parameter of that sign")
                if path in already or any(p == path for p, _, _, _ in plan):
                    raise ValueError("the same parameter twice in one write")
                if dump.get(path) == value:
                    already[path] = value
                    continue
                plan.append((path, (tuple(wp), tuple(idx)), prm, value))
        if not plan:
            return [{"path": p, "value": v, "confirmed": True, "actual": v} for p, v in changes]
        with self._write_lock:
            with self._echo:
                self._awaiting = {key: None for _, key, _, _ in plan}
            echoed: dict[Any, Any] = {}
            try:
                self._authorized = {key: (prm.type, value) for _, key, prm, value in plan}
                try:
                    for _, (wpath, widx), prm, value in plan:
                        self._send(sock, "MMIX", set_param_message(list(wpath), list(widx), prm, value))
                finally:
                    self._authorized = {}
                log.info(
                    "TF5 editor protocol: wrote %d parameter(s) on user request: %s",
                    len(plan),
                    ", ".join(f"{p}={v}" for p, _, _, v in plan),
                )
                deadline = time.monotonic() + timeout
                with self._echo:
                    while any(v is None for v in self._awaiting.values()):
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        self._echo.wait(left)
                    echoed = dict(self._awaiting)
            finally:
                with self._echo:
                    self._awaiting = {}
        sent = {
            path: {"path": path, "value": value, "confirmed": echoed.get(key) is not None, "actual": echoed.get(key)}
            for path, key, _, value in plan
        }
        return [sent.get(p) or {"path": p, "value": v, "confirmed": True, "actual": v} for p, v in changes]

    def _allow_set(self, msg: Message) -> bool:
        """check_outbound's test while write() runs: exactly one of the requested changes."""
        e = msg.elements
        if (
            len(e) != 8
            or e[0] != Element(T_U8, b"\0")
            or e[1] != Element(T_STR, "Mixing")
            or e[2] != Element(T_STR, "Mixing")
            or e[6] != Element(T_U32, [ORIGIN_SELF])
            or e[4].type != T_U32
            or e[5].type != T_U32
            or e[3] != Element(T_U16, [len(e[4].value)])
        ):
            return False
        want = self._authorized.get((tuple(e[4].value), tuple(e[5].value)))
        if want is None:
            return False
        typ, value = want
        return e[7] == Element(T_S32 if typ == 1 else T_U32, [value])

    # ------------------------------------------------------------------ connection thread

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 2.0
            except ReadOnlyViolation as e:
                # A bug in this module: stop for good rather than retry.
                log.error("TF5 editor protocol: refused to send a non-read-only frame (%s); stopped", e)
                self.last_error = f"stopped: {e}"
                self.connected = self.synced = False
                self._sock = None
                return
            except (OSError, ProtocolError) as e:
                if self._stop.is_set():
                    break
                if self.last_error != str(e):
                    log.warning("TF5 editor protocol %s:%d: %s (retrying)", self.host, self.port, e)
                self.last_error = str(e)
            finally:
                self.connected = self.synced = False
                self._sock = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _send(self, sock: socket.socket, tag: str, payload: bytes) -> None:
        """THE ONLY WAY ANYTHING IS SENT. Refuses anything that isn't read-only, except
        the set-params of a write() in progress."""
        check_outbound(tag, payload, self._allow_set if self._authorized else None)
        with self._send_lock:
            sock.sendall(frame(tag, payload))
        self.sent = (self.sent + [(tag, parse_message(payload).cmd)])[-50:]

    def _session(self) -> None:
        reader = FrameReader()
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(1.0)
            self.connected = True
            self.connects += 1
            log.info(
                "TF5 editor protocol: connected to %s:%d (%s)",
                self.host,
                self.port,
                "EQ writes on request" if self.writes else "read-only",
            )
            self._phase, self._mixing_due = "property", None
            self._open("Property", sock)
            started = last_rx = last_ka = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if self._mixing_due is not None and now >= self._mixing_due and self._phase == "client_type":
                    self._start_mixing(sock)
                if not self.synced and now - started > SYNC_TIMEOUT_S:
                    raise TimeoutError(f"no Mixing dump within {SYNC_TIMEOUT_S:.0f} s (phase {self._phase})")
                if now - last_rx > RX_TIMEOUT_S:
                    raise TimeoutError("no data from the console")
                if now - last_ka >= KEEPALIVE_S:
                    self._send(sock, EVENT_TAG, event("KeepAlive"))
                    last_ka = now
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    raise ConnectionError("console closed the connection")
                last_rx = time.monotonic()
                for tag, payload in reader.feed(chunk):
                    self._handle(sock, tag, payload)

    def _open(self, func: str, sock: socket.socket) -> None:
        tag = TAGS[func]
        self._send(sock, tag, subscribe(func))
        self._send(sock, tag, hash_compare(func))
        self._send(sock, tag, sync(close=False))

    def _handle(self, sock: socket.socket, tag: str, payload: bytes) -> None:
        msg = parse_message(payload)
        key = f"{tag} {bytes(msg.cmd).hex(' ')}"
        self.received[key] = self.received.get(key, 0) + 1
        if tag == EVENT_TAG:
            self._on_event(sock, msg)
        elif tag == "MPRO":
            if msg.cmd == DUMP:
                # Contains the console password: drop it unparsed, never log it.
                self._send(sock, "MPRO", sync(close=True))
                if self._phase == "property":
                    self._phase = "client_type"
                    self._send(sock, EVENT_TAG, event("SetClientType", CLIENT_TYPE))
        elif tag == "MMIX":
            if msg.cmd == HASH_COMPARE and len(msg.elements) >= 3 and msg.elements[2].type == T_U8:
                self.desc_md5 = msg.elements[2].value.hex()
                if self.desc_md5 != DESC_MD5["Mixing"]:
                    log.warning(
                        "TF5 Mixing descriptor %s differs from the known firmware; "
                        "EQ is read by parameter name and may be incomplete",
                        self.desc_md5,
                    )
            elif msg.cmd == DUMP:
                self._on_dump(sock, msg)
            elif msg.cmd == SET_PARAM:
                self._on_set(msg)

    def _on_event(self, sock: socket.socket, msg: Message) -> None:
        if len(msg.elements) < 4 or msg.elements[2].type != T_STR:
            return
        name = msg.elements[2].value
        args = msg.elements[3].value if msg.elements[3].type == T_STR else ""
        if msg.cmd == EVENT_REPLY:
            if name == "SetClientType" and self._phase == "client_type" and self._mixing_due is None:
                # TF Editor waits for onSetClientType; fall back if it never comes.
                self._mixing_due = time.monotonic() + CLIENT_TYPE_WAIT_S
            return
        if msg.cmd != EVENT:
            return
        if name in ALLOWED_EVENT_REPLIES:
            self._send(sock, EVENT_TAG, event(name, args, reply=True))  # TF Editor echoes these
        elif name == "onSetClientType":
            if self._phase == "client_type":
                self._start_mixing(sock)
        else:
            log.info("TF5 editor protocol: console event %r not answered", name)

    def _start_mixing(self, sock: socket.socket) -> None:
        self._phase = "mixing"
        self._mixing_due = None
        self._open("Mixing", sock)

    def _on_dump(self, sock: socket.socket, msg: Message) -> None:
        blob = dump_blob(msg)
        self._send(sock, "MMIX", sync(close=True))
        if blob is None:
            raise ProtocolError("Mixing dump without MMSXLIT data")
        dump = Dump(blob)
        if dump.func != "Mixing":
            raise ProtocolError(f"dump for {dump.func!r} on the Mixing channel")
        with self._lock:
            self._mixing = dump
        if not self.synced:
            log.info("TF5 editor protocol: Mixing data received (%d bytes)", len(dump.data))
        self.synced = True
        self._phase = "synced"
        self.last_error = None
        self.updated = time.time()
        self._changed()

    def _on_set(self, msg: Message) -> None:
        # e0 flags, e1 function, e2 base path, e3 depth, e4 path, e5 index, e6 origin, e7 value
        e = msg.elements
        if len(e) < 8 or e[1].value != "Mixing" or e[4].type != T_U32 or e[5].type != T_U32:
            return
        value = e[7].value
        if e[7].type == T_U8:
            if len(value) != 1:
                return
            value = value[0]
        elif e[7].type != T_STR:
            if len(value) != 1:
                return
            value = value[0]
        with self._lock:
            if self._mixing is None:
                return
            try:
                self._mixing.apply(e[4].value, e[5].value, value)
            except ProtocolError as err:
                self.rejected += 1
                self.last_rejected = f"path={e[4].value} idx={e[5].value}: {err}"
                if self.rejected <= 5 or self.rejected % 100 == 0:  # loud, but not one line per knob tick
                    log.warning(
                        "TF5 editor protocol: push not applied (%d so far): %s", self.rejected, self.last_rejected
                    )
                return
        key = (tuple(e[4].value), tuple(e[5].value))
        with self._echo:
            if key in self._awaiting:  # the console confirming a write() of ours
                self._awaiting[key] = value
                self._echo.notify_all()
        self.changes += 1
        self.updated = time.time()
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("TF5 EQ change callback failed")
