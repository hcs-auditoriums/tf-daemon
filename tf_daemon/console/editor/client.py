"""TFEditorClient: one TF Editor protocol session with the console (see __init__)."""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable, Sequence
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
from .guard import (
    ALLOWED_EVENT_REPLIES,
    BASE_FUNCTIONS,
    FAMILIES,
    RECALL_SCENE,
    ReadOnlyViolation,
    WriteRefused,
    check_outbound,
    check_recall,
    check_write,
)
from .tree import function_of_tag

log = logging.getLogger(__name__)

DEFAULT_PORT = 50368
MAX_FRAME = 4 * 1024 * 1024  # the Mixing dump is 67 kB; anything near this is garbage
KEEPALIVE_S = 10.0  # our keepalive period (the guide: every <= 15 s)
RX_TIMEOUT_S = 35.0  # no frame from the console for this long: reconnect
SYNC_TIMEOUT_S = 20.0  # connect -> Mixing dump
CLIENT_TYPE_WAIT_S = 3.0  # SetClientType acked but no onSetClientType: go on after this
WRITE_CONFIRM_S = 3.0  # write(): how long to wait for the console to echo each change
FUNC_TIMEOUT_S = 10.0  # a function after Mixing that sends no dump in this time is skipped
RECALL_CONFIRM_S = 5.0  # recall_scene(): how long to wait for onRecalledScene
Changes = Sequence[tuple[str, int | str]]  # write(): (name path, raw value) pairs
SECRET = re.compile(r"(?:^|/)Password(?::\d+)?$")  # never returned by get() (Setup has StageMix's)


class TFEditorClient:
    """Keeps a TF Editor protocol session; holds the console's data, one Dump per function.

    ``functions``: what to subscribe besides the handshake's Property (whose dump holds the
    console password and is dropped unparsed); Mixing always, the others after it, one at a
    time (config ``tf5.functions``). ``on_change`` (optional) is called with no arguments
    after the Mixing data changed (dump received or a parameter pushed); ``versions[func]``
    counts changes of every function.

    Writing (``write()``, ``recall_scene()``) only on a person's request, and only what the
    enabled write families allow (guard.FAMILIES): ``writes`` enables ``eq``,
    ``fader_writes`` (with ``writes``) ``fader``, ``families`` any others by name;
    ``scene_recall`` allows ``recall_scene()``. With none of them the client is read-only.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        on_change: Callable[[], None] | None = None,
        writes: bool = False,
        fader_writes: bool = False,
        *,
        families: Iterable[str] = (),
        functions: Iterable[str] = BASE_FUNCTIONS,
        scene_recall: bool = False,
    ) -> None:
        self.host, self.port = host, port
        families = set(families)
        unknown = families - set(FAMILIES)
        if unknown:
            raise ValueError(f"unknown write families {sorted(unknown)} (known: {sorted(FAMILIES)})")
        self.writes = writes or "eq" in families
        self.fader_writes = self.writes and (fader_writes or "fader" in families)
        self.families = frozenset(families - {"eq", "fader"})
        self.scene_recall = scene_recall
        wanted = set(functions) | set(BASE_FUNCTIONS)
        self.functions = tuple(f for f in TAGS if f in wanted)  # Property, Mixing, then the rest
        self._write_lock = threading.Lock()  # one write() / recall_scene() at a time
        self._write_func = "Mixing"  # the function of the write() in progress
        self._authorized: dict[tuple[tuple[int, ...], tuple[int, ...]], tuple[int, int | str]] = {}  # addr -> value
        self._recall: str | None = None  # the RecallScene args of the recall_scene() in progress
        self._recalled: str | None = None  # its onRecalledScene STATUS, once it came
        self.last_recalled: dict[str, Any] | None = None  # the desk's last onRecalledScene: number, status, at
        self._echo = threading.Condition()
        self._awaiting: dict[tuple[tuple[int, ...], tuple[int, ...]], int | str | None] = {}  # addr -> echoed value
        self.on_change = on_change
        self._lock = threading.Lock()
        self._dumps: dict[str, Dump] = {}
        self.versions: dict[str, int] = {f: 0 for f in self.functions if f != "Property"}
        self.descriptors: dict[str, str] = {}  # function -> descriptor MD5 the console reported
        self.skipped: set[str] = set()  # functions that sent no dump this session
        self._queue: list[str] = []  # functions still to open after Mixing
        self._opening: str | None = None
        self._func_due: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.connected = False
        self.synced = False
        self.connects = 0
        self.last_error: str | None = None
        self.updated: float | None = None
        self.changes = 0  # Mixing parameter pushes applied
        self.rejected = 0  # pushes that didn't fit our copy of the data (should stay 0)
        self.last_rejected: str | None = None
        self.received: dict[str, int] = {}  # "TAG cc dd ee" -> frames received (diagnostics)
        self.desc_md5: str | None = None
        self.sent: list[tuple[str, tuple[int, int, int]]] = []  # (tag, cmd) of the last frames sent
        self._phase = "idle"
        self._mixing_due: float | None = None

    @property
    def enabled_families(self) -> frozenset[str]:
        """The write families this client may use (guard.FAMILIES names)."""
        base = {"eq"} if self.writes else set()
        if self.writes and self.fader_writes:
            base.add("fader")
        return self.families | base

    @property
    def _mixing(self) -> Dump | None:
        return self._dumps.get("Mixing")

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
        enabled = self.enabled_families
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
            "read_only": not enabled and not self.scene_recall,
            "fader_writes": self.fader_writes,
            "write_families": sorted(enabled),
            "scene_recall": self.scene_recall,
            "functions": {
                f: {
                    "synced": f in self._dumps,
                    "skipped": f in self.skipped,
                    "descriptor_md5": self.descriptors.get(f),
                    "descriptor_known": self.descriptors.get(f) in (None, DESC_MD5[f]),
                    "version": self.versions.get(f, 0),
                }
                for f in self.functions
                if f != "Property"
            },
        }

    def eq(self, kind: str, x: int) -> dict[str, Any] | None:
        with self._lock:
            mixing = self._mixing
            return channel_eq(mixing, kind, x) if mixing is not None else None

    def get(self, path: str, func: str = "Mixing") -> int | str | None:
        """Any parameter by function and name path, e.g. ``"InputChannel:0/Label/Name"``;
        None if unknown (not subscribed, not received yet, no such parameter) or a password."""
        if SECRET.search(path):
            return None
        with self._lock:
            dump = self._dumps.get(func)
            if dump is None:
                return None
            try:
                return dump.get(path)
            except KeyError:
                return None

    def write(self, changes: Changes, timeout: float = WRITE_CONFIRM_S, func: str = "Mixing") -> list[dict[str, Any]]:
        """CHANGE PARAMETERS ON THE CONSOLE: ``[(name path, raw value), ...]`` in ``func``.

        Only for an explicit request by a person; nothing calls this on its own. Only what an
        enabled write family allows (guard.check_write: path, range, allowed strings); anything
        else raises before a byte is sent (WriteRefused / WriteNotAllowed, both ValueErrors;
        PermissionError with no family enabled at all). Returns per change whether the console
        echoed it back within ``timeout`` and the value it reported. A change to the value the
        console already has isn't sent (the TF5 doesn't echo a set that changes nothing, so it
        could never be confirmed); it's reported as confirmed.
        """
        enabled = self.enabled_families
        if not enabled:
            raise PermissionError("writing to the console is off (tf5.eq_write and the other tf5.*_write are false)")
        sock = self._sock
        plan: list[tuple[str, tuple[tuple[int, ...], tuple[int, ...]], Any, int | str]] = []
        already: dict[str, int | str] = {}  # path -> value the console has already
        with self._lock:
            dump = self._dumps.get(func)
            if not self.synced or sock is None:
                raise ConnectionError("not connected to the console's editor protocol")
            for path, value in changes:
                check_write(func, path, value, enabled)
                if dump is None:
                    raise WriteRefused(f"{func}: no data from the console (is it in tf5.functions?)")
                try:
                    wp, idx, prm = dump.address(path)
                except KeyError:
                    raise WriteRefused(f"{path}: not on this console") from None
                if (prm.type == 0) != isinstance(value, str) or (
                    prm.type == 2 and isinstance(value, int) and value < 0
                ):
                    raise WriteRefused(f"{path}: not a parameter of that type / sign on this console")
                if path in already or any(p == path for p, _, _, _ in plan):
                    raise WriteRefused("the same parameter twice in one write")
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
                self._write_func = func
                self._authorized = {key: (prm.type, value) for _, key, prm, value in plan}
                try:
                    for _, (wpath, widx), prm, value in plan:
                        self._send(sock, TAGS[func], set_param_message(list(wpath), list(widx), prm, value, func))
                finally:
                    self._authorized = {}
                log.info(
                    "TF5 editor protocol: wrote %d %s parameter(s) on user request: %s",
                    len(plan),
                    func,
                    ", ".join(f"{p}={v}" for p, _, _, v in plan),
                )
                echoed = self._wait(lambda: all(v is not None for v in self._awaiting.values()), timeout)
            finally:
                with self._echo:
                    self._awaiting = {}
                    self._write_func = "Mixing"
        sent = {
            path: {"path": path, "value": value, "confirmed": echoed.get(key) is not None, "actual": echoed.get(key)}
            for path, key, _, value in plan
        }
        return [sent.get(p) or {"path": p, "value": v, "confirmed": True, "actual": v} for p, v in changes]

    def recall_scene(self, number: str, timeout: float = RECALL_CONFIRM_S) -> dict[str, Any]:
        """RECALL A SCENE ON THE CONSOLE: ``number`` as the desk shows it ("A05", "B12").

        Only for an explicit request by a person, with ``scene_recall`` on. Sends ``RecallScene
        "NUMBER:<number>"`` and waits for the console's ``onRecalledScene`` for that number;
        returns ``{"number", "confirmed", "status"}`` (status: the console's STATUS token)."""
        if not self.scene_recall:
            raise PermissionError("recalling scenes is off (tf5.scene_recall = false)")
        args = f"NUMBER:{number}"
        check_recall(args)
        sock = self._sock
        if not self.synced or sock is None:
            raise ConnectionError("not connected to the console's editor protocol")
        with self._write_lock:
            with self._echo:
                self._recall, self._recalled = args, None
            try:
                self._send(sock, EVENT_TAG, event(RECALL_SCENE, args))
                log.info("TF5 editor protocol: recalled scene %s on user request", number)
                self._wait(lambda: self._recalled is not None, timeout)
                status = self._recalled
            finally:
                with self._echo:
                    self._recall = None
        return {"number": number, "confirmed": status is not None, "status": status}

    def _wait(self, done: Callable[[], bool], timeout: float) -> dict[Any, Any]:
        """Wait (on the echo condition) until ``done()`` or ``timeout``; the echoes so far."""
        deadline = time.monotonic() + timeout
        with self._echo:
            while not done():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._echo.wait(left)
            return dict(self._awaiting)

    def _allow_set(self, msg: Message) -> bool:
        """check_outbound's test while write() runs: exactly one of the requested changes."""
        e = msg.elements
        func = self._write_func
        if (
            len(e) != 8
            or e[0] != Element(T_U8, b"\0")
            or e[1] != Element(T_STR, func)
            or e[2] != Element(T_STR, func)
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
        if typ == 0:
            return e[7] == Element(T_STR, value)
        return e[7] == Element(T_S32 if typ == 1 else T_U32, [value])

    def _allow_event(self, name: str, args: str) -> bool:
        """check_outbound's test while recall_scene() runs: exactly the requested recall."""
        return name == RECALL_SCENE and self._recall is not None and args == self._recall

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
        check_outbound(
            tag,
            payload,
            self._allow_set if self._authorized else None,
            self.functions,
            self._allow_event if self._recall is not None else None,
        )
        with self._send_lock:
            sock.sendall(frame(tag, payload))
        self.sent = (self.sent + [(tag, parse_message(payload).cmd)])[-50:]

    def _session(self) -> None:
        reader = FrameReader()
        with socket.create_connection((self.host, self.port), timeout=5) as sock:
            self._sock = sock
            sock.settimeout(1.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # small frames, sent at once
            self.connected = True
            self.connects += 1
            log.info(
                "TF5 editor protocol: connected to %s:%d (%s)",
                self.host,
                self.port,
                "EQ writes on request" if self.writes else "read-only",
            )
            self._phase, self._mixing_due = "property", None
            self._queue, self._opening, self._func_due = [], None, None
            self.skipped = set()
            self._open("Property", sock)
            started = last_rx = last_ka = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if self._mixing_due is not None and now >= self._mixing_due and self._phase == "client_type":
                    self._start_mixing(sock)
                if self._opening is not None and self._func_due is not None and now >= self._func_due:
                    log.warning("TF5 editor protocol: no %s dump in %.0f s; skipped", self._opening, FUNC_TIMEOUT_S)
                    self.skipped.add(self._opening)
                    self._open_next(sock)
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
            return
        func = function_of_tag(tag)
        if func is None or func not in self.functions:
            return
        if func == "Property":
            if msg.cmd == DUMP:
                # Contains the console password: drop it unparsed, never log it.
                self._send(sock, tag, sync(close=True))
                if self._phase == "property":
                    self._phase = "client_type"
                    self._send(sock, EVENT_TAG, event("SetClientType", CLIENT_TYPE))
        elif msg.cmd == HASH_COMPARE and len(msg.elements) >= 3 and msg.elements[2].type == T_U8:
            md5 = msg.elements[2].value.hex()
            self.descriptors[func] = md5
            if func == "Mixing":
                self.desc_md5 = md5
            if md5 != DESC_MD5[func]:
                log.warning(
                    "TF5 %s descriptor %s differs from the known firmware; parameters are read by name "
                    "and may be incomplete",
                    func,
                    md5,
                )
        elif msg.cmd == DUMP:
            self._on_dump(sock, func, msg)
        elif msg.cmd == SET_PARAM:
            self._on_set(func, msg)

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
        elif name == "onRecalledScene":
            tokens = dict(t.partition(":")[::2] for t in args.split())
            log.info("TF5 editor protocol: scene %s recalled (%s)", tokens.get("NUMBER"), tokens.get("STATUS"))
            self.last_recalled = {"number": tokens.get("NUMBER"), "status": tokens.get("STATUS"), "at": time.time()}
            with self._echo:
                if self._recall is not None and tokens.get("NUMBER") == self._recall[7:]:
                    self._recalled = tokens.get("STATUS", "")
                    self._echo.notify_all()
        else:
            log.info("TF5 editor protocol: console event %r not answered", name)

    def _start_mixing(self, sock: socket.socket) -> None:
        self._phase = "mixing"
        self._mixing_due = None
        self._open("Mixing", sock)

    def _open_next(self, sock: socket.socket) -> None:
        """Subscribe the next function after Mixing (one at a time, each with its own dump)."""
        self._opening = self._queue.pop(0) if self._queue else None
        self._func_due = time.monotonic() + FUNC_TIMEOUT_S if self._opening else None
        if self._opening is not None:
            self._open(self._opening, sock)

    def _on_dump(self, sock: socket.socket, func: str, msg: Message) -> None:
        blob = dump_blob(msg)
        self._send(sock, TAGS[func], sync(close=True))
        if blob is None:
            raise ProtocolError(f"{func} dump without MMSXLIT data")
        dump = Dump(blob)
        if dump.func != func:
            raise ProtocolError(f"dump for {dump.func!r} on the {func} channel")
        with self._lock:
            self._dumps[func] = dump
        self.versions[func] = self.versions.get(func, 0) + 1
        self.updated = time.time()
        if func != "Mixing":
            log.info("TF5 editor protocol: %s data received (%d bytes)", func, len(dump.data))
            if func == self._opening:
                self._open_next(sock)
            return
        if not self.synced:
            log.info("TF5 editor protocol: Mixing data received (%d bytes)", len(dump.data))
        self.synced = True
        self.last_error = None
        if self._phase == "mixing":  # the session's first Mixing dump: now the other functions
            self._queue = [f for f in self.functions if f not in BASE_FUNCTIONS]
            self._open_next(sock)
        self._phase = "synced"
        self._changed()

    def _on_set(self, func: str, msg: Message) -> None:
        # e0 flags, e1 function, e2 base path, e3 depth, e4 path, e5 index, e6 origin, e7 value
        e = msg.elements
        if len(e) < 8 or e[1].value != func or e[4].type != T_U32 or e[5].type != T_U32:
            return
        value = e[7].value
        if e[7].type != T_STR:
            if len(value) != 1:
                return
            value = value[0]
        with self._lock:
            dump = self._dumps.get(func)
            if dump is None:
                return
            try:
                dump.apply(e[4].value, e[5].value, value)
            except ProtocolError as err:
                self.rejected += 1
                self.last_rejected = f"{func} path={e[4].value} idx={e[5].value}: {err}"
                if self.rejected <= 5 or self.rejected % 100 == 0:  # loud, but not one line per knob tick
                    log.warning(
                        "TF5 editor protocol: push not applied (%d so far): %s", self.rejected, self.last_rejected
                    )
                return
        key = (tuple(e[4].value), tuple(e[5].value))
        with self._echo:
            if func == self._write_func and key in self._awaiting:  # the console confirming a write() of ours
                self._awaiting[key] = value
                self._echo.notify_all()
        self.versions[func] = self.versions.get(func, 0) + 1
        self.updated = time.time()
        if func == "Mixing":
            self.changes += 1
            self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("TF5 EQ change callback failed")
