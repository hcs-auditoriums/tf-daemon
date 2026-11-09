"""One monitored receiver: a TCP connection thread that keeps its state fresh.

Read-only by design: only GET requests are sent. On connect (and every
``full_poll_s`` after) it asks for everything; meters (RF level, antenna,
audio level) are polled every ``meter_poll_s``. Unsolicited REP messages the
receiver pushes on its own are applied as they arrive. On any error the
connection is dropped and retried with backoff.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from . import protocol as p
from .network import Interface, bind_to_interface

log = logging.getLogger(__name__)

FULL_QUERY = (
    p.get("DEVICE_ID", None),
    p.get("MODEL", None),
    p.get("FW_VER", None),
    p.get("ALL", 1),
)
METER_QUERY = (p.get("RX_RF_LVL"), p.get("RF_ANTENNA"), p.get("AUDIO_LVL"))
IDLE_TIMEOUT_S = 10.0  # no bytes for this long -> assume the link is dead


def blank_state() -> dict[str, Any]:
    return {
        "mac": None,
        "device_id": None,
        "model": None,
        "fw_ver": None,
        "name": None,
        "frequency_mhz": None,
        "group_chan": None,
        "audio_gain_db": None,
        "interference": False,
        "encryption_warning": False,
        "tx_type": None,
        "tx_rf_power": None,
        "tx_muted": False,
        "batt_type": None,
        "batt_bars": None,
        "batt_charge": None,
        "batt_run_min": None,
        "batt_calculating": False,
        "rf_dbm": None,
        "ant_a": False,
        "ant_b": False,
        "audio_dbfs": None,
    }


class Receiver:
    def __init__(
        self,
        host: str,
        port: int,
        iface: Interface | None,
        on_change: Callable[[], None],
        meter_poll_s: float = 1.0,
        full_poll_s: float = 10.0,
        connect_timeout_s: float = 3.0,
    ) -> None:
        self.host, self.port, self.iface = host, port, iface
        self.on_change = on_change
        self.meter_poll_s, self.full_poll_s, self.connect_timeout_s = meter_poll_s, full_poll_s, connect_timeout_s

        self._lock = threading.Lock()
        self.state = blank_state()
        self.online = False
        self.ever_online = False  # answered at least once: it is a receiver
        self.failures = 0  # consecutive failed connection attempts
        self.error: str | None = None
        self.last_seen: float | None = None

        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"qlxd-{host}:{port}", daemon=True)

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def signal_stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def join(self, timeout: float = 5.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def stop(self) -> None:
        self.signal_stop()
        self.join()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "key": self.key,
                "host": self.host,
                "port": self.port,
                "online": self.online,
                "error": self.error,
                "last_seen": self.last_seen,
                **self.state,
            }

    # ------------------------------------------------------------------ thread

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except OSError as e:
                with self._lock:
                    was_online, self.online = self.online, False
                    self.failures += 1
                    self.error = str(e) or type(e).__name__
                if was_online:
                    log.info("QLX-D %s offline: %s", self.key, self.error)
                self.on_change()
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 15.0)

    def _session(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock = sock
        try:
            bind_to_interface(sock, self.iface)
            sock.settimeout(self.connect_timeout_s)
            sock.connect((self.host, self.port))
            sock.settimeout(0.25)
            reader = p.MessageReader()
            sock.sendall(b"".join(FULL_QUERY))
            last_full = last_meter = time.monotonic()
            last_rx = time.monotonic()
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_full >= self.full_poll_s:
                    sock.sendall(b"".join(FULL_QUERY))
                    last_full = now
                if now - last_meter >= self.meter_poll_s:
                    sock.sendall(b"".join(METER_QUERY))
                    last_meter = now
                try:
                    data = sock.recv(4096)
                except TimeoutError:
                    if now - last_rx > IDLE_TIMEOUT_S:
                        raise TimeoutError("no response from receiver") from None
                    continue
                if not data:
                    raise ConnectionResetError("receiver closed the connection")
                last_rx = time.monotonic()
                self._apply(reader.feed(data))
        finally:
            self._sock = None
            sock.close()

    def _apply(self, bodies: list[str]) -> None:
        changes: dict[str, Any] = {}
        parsed = False
        for body in bodies:
            msg = p.parse(body)
            if msg is not None:
                parsed = True
                _, _, param, args = msg
                changes.update(p.decode(param, args))
        if not parsed:  # nothing protocol-shaped (yet): not proof this is a receiver
            return
        with self._lock:
            first = not self.online
            self.state.update(changes)
            self.online = self.ever_online = True
            self.failures, self.error = 0, None
            self.last_seen = time.time()
        if first:
            log.info("QLX-D %s online", self.key)
        self.on_change()
