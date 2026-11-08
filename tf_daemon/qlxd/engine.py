"""QLX-D engine: persisted network settings, discovery, receiver set, state hub.

Receivers come from three places:
  * manual   - hosts listed in the settings (always shown, online or not)
  * found    - SLP announcements heard on the selected interface; shown once
               they have answered on TCP 2202, dropped if they never do
  * demo     - in-process simulators on 127.0.0.1 (``qlxd.source = "demo"``)

Settings (interface + manual hosts) are edited from the UI and saved as JSON
in ``qlxd.state_file`` so they survive restarts; config.toml only seeds them.

The same file holds the *known receivers* catalog: every receiver ever seen,
keyed by MAC address (falling back to host:port), with its channel name,
device ID and frequency. Other pages use it (``frequencies()``; the /rf page
draws receiver frequencies) even while a receiver is offline or before it
has been reached since a restart.

Every published message is a full JSON snapshot (text over /ws/qlxd),
throttled to a few per second:

    {"type": "state", "demo": bool, "settings": {"interface", "hosts"},
     "network": {"interface", "ip", "error", "slp_packets"},
     "receivers": [{"key", "host", "port", "source", "online", "error", "last_seen",
                    "name", "frequency_mhz", "batt_bars", "batt_run_min", "rf_dbm", ...}]}
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from ..hub import FrameHub
from . import protocol
from .network import Interface, SLPListener, find_interface, list_interfaces, source_ip
from .receiver import Receiver
from .simulator import SimReceiver, demo_receivers

log = logging.getLogger(__name__)

PUBLISH_MIN_INTERVAL_S = 0.25
KNOWN_SAVE_MIN_INTERVAL_S = 5.0  # catalog changes are written at most this often
PROBE_ATTEMPTS = 3          # a found host that never answers this many times is dropped...
REJECT_RETRY_S = 600.0      # ...and ignored for this long
_HOST = re.compile(r"^[A-Za-z0-9.\-]+(?::\d{1,5})?$")


def parse_host(entry: str) -> tuple[str, int]:
    entry = entry.strip()
    if not _HOST.match(entry):
        raise ValueError(f"invalid host {entry!r} (expected IP or IP:port)")
    host, _, port = entry.partition(":")
    port_n = int(port) if port else protocol.PORT
    if not 0 < port_n < 65536:
        raise ValueError(f"invalid port in {entry!r}")
    return host, port_n


class QLXDEngine:
    def __init__(self, cfg: dict[str, Any], demo: bool = False) -> None:
        q = cfg["qlxd"]
        self.demo = demo
        self.meter_poll_s = float(q["meter_poll_s"])
        self.full_poll_s = float(q["full_poll_s"])
        self.state_path = Path(q["state_file"])
        data = self._read_state()
        self.settings = self._settings_from(data, {"interface": str(q["interface"]), "hosts": list(q["hosts"])})
        self._known: dict[str, dict[str, Any]] = self._known_from(data)
        self._known_dirty = False
        self._known_saved_at = 0.0
        self.hub = FrameHub()

        self._lock = threading.RLock()       # guards the receiver set and network state
        self._apply_lock = threading.Lock()  # serializes rebuilds; never taken by callbacks
        self._generation = 0                 # bumped per rebuild; stale listeners are ignored
        self._receivers: dict[str, tuple[Receiver, str]] = {}  # key -> (receiver, source)
        self._rejected: dict[str, float] = {}                    # key -> monotonic time
        self._iface: Interface | None = None
        self._net_error: str | None = None
        self._listener: SLPListener | None = None
        self._sims: list[SimReceiver] = []

        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._publish()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.demo:
            self._sims = demo_receivers()
            for s in self._sims:
                s.start()
        self._apply_network()
        self._thread = threading.Thread(target=self._run, name="qlxd-publish", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()
        with self._apply_lock:
            self._teardown()
        for s in self._sims:
            s.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._save_known(force=True)
        self.hub.close()

    # ------------------------------------------------------------------ settings

    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist and apply ``{"interface": str, "hosts": [str]}`` (either key optional)."""
        new = dict(self.settings)
        if "interface" in body:
            iface = body["interface"]
            if iface is None:
                iface = ""
            if not isinstance(iface, str):
                raise ValueError("interface must be a string ('' = auto)")
            if iface and iface not in {i.name for i in list_interfaces()}:
                raise ValueError(f"unknown interface {iface!r}")
            new["interface"] = iface
        if "hosts" in body:
            hosts = body["hosts"]
            if isinstance(hosts, str):
                hosts = re.split(r"[\s,]+", hosts)
            if not isinstance(hosts, list):
                raise ValueError("hosts must be a list")
            clean = []
            for h in hosts:
                if str(h).strip():
                    host, port = parse_host(str(h))
                    entry = host if port == protocol.PORT else f"{host}:{port}"
                    if entry not in clean:
                        clean.append(entry)
            new["hosts"] = clean
        with self._lock:
            self._write_state(new, self._known)
            self.settings = new
        self._apply_network()
        return new

    def rescan(self) -> None:
        """Forget found receivers and rejected hosts, then rediscover."""
        with self._lock:
            self._rejected.clear()
        self._apply_network()

    def _read_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable %s: %s", self.state_path, e)
            return {}
        if not isinstance(data, dict):
            log.warning("Ignoring %s: not a JSON object", self.state_path)
            return {}
        log.info("Loaded QLX-D state from %s", self.state_path)
        return data

    @staticmethod
    def _settings_from(data: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
        out = dict(defaults)
        if isinstance(data.get("interface"), str):
            out["interface"] = data["interface"]
        if isinstance(data.get("hosts"), list):
            out["hosts"] = [str(h) for h in data["hosts"]]
        return out

    @staticmethod
    def _known_from(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        known = data.get("known_receivers")
        if not isinstance(known, dict):
            return {}
        out = {}
        for key, e in known.items():
            if isinstance(e, dict) and isinstance(e.get("frequency_mhz"), (int, float)):
                out[str(key)] = {
                    "name": e.get("name") if isinstance(e.get("name"), str) else None,
                    "device_id": e.get("device_id") if isinstance(e.get("device_id"), str) else None,
                    "frequency_mhz": float(e["frequency_mhz"]),
                    "host": e.get("host") if isinstance(e.get("host"), str) else None,
                    "last_seen": e.get("last_seen") if isinstance(e.get("last_seen"), (int, float)) else None,
                }
        return out

    def _write_state(self, settings: dict[str, Any], known: dict[str, dict[str, Any]]) -> None:
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        data = {**settings, "known_receivers": known}
        try:
            if self.state_path.parent != Path("."):
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            raise ValueError(f"could not save settings to {self.state_path}: {e}") from e

    # ------------------------------------------------------------------ known receivers

    @staticmethod
    def _known_key(snap: dict[str, Any]) -> str:
        return snap["mac"] or snap["key"]

    def _update_known(self) -> None:
        """Fold the live receivers' name / device ID / frequency into the catalog."""
        with self._lock:
            for r, _src in self._receivers.values():
                snap = r.snapshot()
                if not snap["online"] or snap["frequency_mhz"] is None:
                    continue
                key = self._known_key(snap)
                entry = self._known.get(key, {})
                new = {"name": snap["name"], "device_id": snap["device_id"],
                       "frequency_mhz": snap["frequency_mhz"], "host": snap["host"]}
                if any(entry.get(k) != v for k, v in new.items()):
                    self._known_dirty = True
                self._known[key] = {**new, "last_seen": snap["last_seen"]}  # last_seen alone isn't worth a write

    def _save_known(self, force: bool = False) -> None:
        with self._lock:
            if not self._known_dirty:
                return
            if not force and time.monotonic() - self._known_saved_at < KNOWN_SAVE_MIN_INTERVAL_S:
                return
            try:
                self._write_state(self.settings, self._known)
            except ValueError as e:
                log.warning("%s", e)
                return
            self._known_dirty = False
            self._known_saved_at = time.monotonic()

    def forget_known(self, key: str | None = None) -> int:
        """Drop one catalog entry (or all with ``key=None``); returns how many were removed."""
        with self._lock:
            if key is None:
                n = len(self._known)
                self._known.clear()
            else:
                n = 1 if self._known.pop(key, None) is not None else 0
            if n:
                self._known_dirty = True
        self._save_known(force=True)
        return n

    def frequencies(self) -> list[dict[str, Any]]:
        """Known receivers for other modules: ``[{key, name, device_id, frequency_mhz, online, last_seen}]``.

        Live values win over the catalog, so a just-retuned receiver shows its
        new frequency immediately.
        """
        with self._lock:
            live = {}
            for r, _src in self._receivers.values():
                snap = r.snapshot()
                if snap["online"] and snap["frequency_mhz"] is not None:
                    live[self._known_key(snap)] = snap
            out = []
            for key in self._known.keys() | live.keys():
                e = live.get(key) or self._known[key]
                out.append({"key": key, "name": e["name"], "device_id": e["device_id"],
                            "frequency_mhz": e["frequency_mhz"], "online": key in live,
                            "last_seen": e["last_seen"]})
        return sorted(out, key=lambda d: d["frequency_mhz"])

    # ------------------------------------------------------------------ network / receivers

    def _teardown(self) -> None:
        """Detach the listener and receivers under the lock, then stop them outside it
        (their threads call back into the engine and may be waiting for the lock)."""
        with self._lock:
            self._generation += 1
            listener, self._listener = self._listener, None
            receivers = [r for r, _ in self._receivers.values()]
            self._receivers.clear()
        if listener is not None:
            listener.stop()
        for r in receivers:
            r.signal_stop()
        for r in receivers:
            r.join()

    def _apply_network(self) -> None:
        """(Re)build the listener and receiver set from the current settings."""
        with self._apply_lock:
            self._teardown()
            self._build()
        self._dirty.set()

    def _build(self) -> None:
        with self._lock:
            name = self.settings["interface"]
            self._iface = find_interface(name)
            if self._iface is None:
                self._net_error = (f"interface {name!r} not found" if name
                                   else "no interface with a link-local (169.254.x.x) address; pick one")
            elif not self._iface.ipv4:
                self._net_error = f"{self._iface.name} has no IPv4 address"
            else:
                self._net_error = None

            for entry in self.settings["hosts"]:
                try:
                    host, port = parse_host(entry)
                except ValueError as e:
                    log.warning("%s", e)
                    continue
                self._add(host, port, "manual")
            if self.demo:
                for s in self._sims:
                    self._add("127.0.0.1", s.port, "demo", bind=False)
            elif self._iface is not None and self._iface.ipv4:
                gen = self._generation
                self._listener = SLPListener(self._iface, lambda ip: self._on_announce(ip, gen))
                self._listener.start()

    def _add(self, host: str, port: int, source: str, bind: bool = True) -> None:
        key = f"{host}:{port}"
        if key in self._receivers:
            return
        r = Receiver(host, port, self._iface if bind else None, self._dirty.set,
                     meter_poll_s=self.meter_poll_s, full_poll_s=self.full_poll_s)
        self._receivers[key] = (r, source)
        r.start()

    def _on_announce(self, ip: str, generation: int) -> None:
        key = f"{ip}:{protocol.PORT}"
        with self._lock:
            if generation != self._generation or key in self._receivers:
                return
            t = self._rejected.get(key)
            if t is not None and time.monotonic() - t < REJECT_RETRY_S:
                return
            self._rejected.pop(key, None)
            log.info("Probing announced device %s", ip)
            self._add(ip, protocol.PORT, "found")

    def _prune(self) -> None:
        with self._lock:
            dead = [k for k, (r, src) in self._receivers.items()
                    if src == "found" and not r.ever_online and r.failures >= PROBE_ATTEMPTS]
            for k in dead:
                r, _ = self._receivers.pop(k)
                r.signal_stop()
                self._rejected[k] = time.monotonic()
                log.info("%s did not answer as a receiver; ignoring it for %d min", k, REJECT_RETRY_S // 60)

    # ------------------------------------------------------------------ state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            receivers = []
            for r, src in self._receivers.values():
                if src == "found" and not r.ever_online:
                    continue  # still probing; may not be a receiver at all
                receivers.append({**r.snapshot(), "source": src})
            listener = self._listener
            return {
                "type": "state",
                "demo": self.demo,
                "settings": dict(self.settings),
                "network": {
                    "interface": self._iface.name if self._iface else None,
                    "ip": source_ip(self._iface),
                    "error": self._net_error or (listener.error if listener else None),
                    "slp_packets": listener.packets if listener else 0,
                    "probing": sum(1 for r, s in self._receivers.values() if s == "found" and not r.ever_online),
                },
                "receivers": sorted(receivers, key=lambda d: ((d["name"] or "~").casefold(), d["host"], d["port"])),
            }

    def status(self) -> dict[str, Any]:
        snap = self.snapshot()
        rx = snap["receivers"]
        return {"interface": snap["network"]["interface"], "receivers": len(rx),
                "online": sum(1 for r in rx if r["online"]), "error": snap["network"]["error"]}

    def interfaces(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in list_interfaces()]

    def _run(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            self._dirty.wait(timeout=2.0)  # also republish now and then for "last seen" ages
            if self._stop.is_set():
                return
            wait = PUBLISH_MIN_INTERVAL_S - (time.monotonic() - last)
            if wait > 0:
                self._stop.wait(wait)
            self._dirty.clear()
            self._prune()
            self._update_known()
            self._save_known()
            self._publish()
            last = time.monotonic()

    def _publish(self) -> None:
        self.hub.publish(json.dumps(self.snapshot(), separators=(",", ":")))
