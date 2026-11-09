"""Network helpers: interface listing, NIC-bound sockets, receiver discovery.

Discovery is passive: Shure network devices periodically announce themselves
with SLP multicast (239.255.254.253:8427). Every source address heard on the
selected interface becomes a *candidate*; the engine then probes it on TCP 2202
and only keeps hosts that answer like a receiver.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import struct
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

import psutil

log = logging.getLogger(__name__)

SLP_GROUP = "239.255.254.253"
SLP_PORT = 8427


@dataclass(frozen=True)
class Interface:
    name: str
    ipv4: tuple[str, ...]
    up: bool

    @property
    def link_local(self) -> bool:
        return any(ipaddress.ip_address(a).is_link_local for a in self.ipv4)

    def as_dict(self) -> dict:
        return {"name": self.name, "ipv4": list(self.ipv4), "up": self.up, "link_local": self.link_local}


def list_interfaces() -> list[Interface]:
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        v4 = tuple(a.address for a in addrs if a.family == socket.AF_INET and not a.address.startswith("127."))
        st = stats.get(name)
        if v4 or (st and st.isup):
            out.append(Interface(name, v4, bool(st and st.isup)))
    return sorted(out, key=lambda i: (not i.link_local, i.name))


# Virtual adapters that also carry 169.254.x.x addresses; never auto-selected.
_VIRTUAL = re.compile(
    r"tailscale|zerotier|^zt|docker|^br-|^veth|^virbr|vethernet|^wg|^tun|^tap|bluetooth|loopback", re.I
)


def find_interface(name: str | None) -> Interface | None:
    """The named interface, or (``name`` empty) the first physical, up one with a link-local address."""
    ifaces = list_interfaces()
    if name:
        return next((i for i in ifaces if i.name == name), None)
    return next((i for i in ifaces if i.link_local and i.up and not _VIRTUAL.search(i.name)), None)


def source_ip(iface: Interface | None) -> str | None:
    """The address to bind outgoing connections to (link-local preferred)."""
    if iface is None or not iface.ipv4:
        return None
    ll = [a for a in iface.ipv4 if ipaddress.ip_address(a).is_link_local]
    return (ll or list(iface.ipv4))[0]


def bind_to_interface(sock: socket.socket, iface: Interface | None) -> None:
    """Pin ``sock`` to ``iface``: SO_BINDTODEVICE on Linux, else bind the source address."""
    if iface is None:
        return
    if sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.name.encode() + b"\0")
            return
        except OSError as e:  # needs CAP_NET_RAW on kernels < 5.7
            log.debug("SO_BINDTODEVICE %s failed (%s); binding source address instead", iface.name, e)
    ip = source_ip(iface)
    if ip:
        sock.bind((ip, 0))


class SLPListener:
    """Calls ``on_host(ip)`` for every SLP multicast sender heard on ``iface``."""

    def __init__(self, iface: Interface, on_host: Callable[[str], None]) -> None:
        self.iface = iface
        self.on_host = on_host
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None
        self.packets = 0

    def start(self) -> None:
        ip = source_ip(self.iface)
        if ip is None:
            self.error = f"{self.iface.name} has no IPv4 address"
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", SLP_PORT))
            s.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                struct.pack("4s4s", socket.inet_aton(SLP_GROUP), socket.inet_aton(ip)),
            )
            s.settimeout(1.0)
        except OSError as e:
            self.error = f"cannot listen for SLP on {self.iface.name} ({ip}): {e}"
            log.warning("%s", self.error)
            return
        self._sock = s
        self._thread = threading.Thread(target=self._run, name="qlxd-slp", daemon=True)
        self._thread.start()
        log.info("Listening for Shure SLP announcements on %s (%s)", self.iface.name, ip)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._sock is not None:
            self._sock.close()

    def _run(self) -> None:
        own = set(self.iface.ipv4)
        sock = self._sock
        assert sock is not None  # start() opens it before starting this thread
        while not self._stop.is_set():
            try:
                _, (addr, _) = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            self.packets += 1
            if addr not in own:
                self.on_host(addr)
