"""Simulated TF5 (demo mode, development and tests): both of its network protocols.

    python -m tf_daemon.console.sim                 # RCP on 49280, TF Editor on 50368
    python -m tf_daemon --demo                      # the whole app, this console in-process

* SimRCP: the console side of RCP (console/rcp.py): answers ``prminfo`` / ``get`` with a
  plausible service's channel list and pushes ``NOTIFY set`` when a fader moves
  "on the desk".
* SimEditor: the console side of the TF Editor handshake recorded from TF
  Editor (guides/tf5_api.md, section 6), with synthetic MMSXLIT dumps. It applies
  and echoes set-params like the TF5, so "Apply to desk" works against it, and
  records every frame the client sends so tests can check that nothing but
  read-only handshake traffic left the client.

The MMSXLIT builder covers the subset of the Mixing tree tf-daemon reads, with
the TF5's parameter names, widths and InputChannel collection order, so real
wire addresses resolve.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import random
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .editor.codec import (
    DESC_MD5,
    DUMP,
    EVENT,
    HASH_COMPARE,
    SET_PARAM,
    SYNC_CLOSE,
    SYNC_OPEN,
    T_S32,
    T_STR,
    T_U8,
    T_U16,
    T_U32,
    FrameReader,
    Message,
    element,
    event,
    frame,
    message,
    parse_message,
)
from .editor.dump import Dump, channel_eq

log = logging.getLogger(__name__)

STR, INT, UINT = 0, 1, 2
PASSWORD = "s3cret-Pa55word"   # in the fake Property dump; must never leak anywhere


# ---------------------------------------------------------------------- MMSXLIT builder

@dataclass
class C:
    """A collection: name, array size, params [(name, type, elemsize, arraysize)], children."""
    name: str
    arraysize: int = 1
    params: list[tuple[str, int, int, int]] = field(default_factory=list)
    children: list[C] = field(default_factory=list)


def _p(name: str, typ: int, size: int, arr: int = 1) -> tuple[str, int, int, int]:
    return (name, typ, size, arr)


def _datasize(c: C) -> int:
    return sum(p[2] * p[3] for p in c.params) + sum(_datasize(ch) * ch.arraysize for ch in c.children)


def _desc(c: C, offset: int) -> bytes:
    prs = b"".join(b"PR " + struct.pack("<BHH", t, size, arr) + name.encode().ljust(24, b"\0")
                   for name, t, size, arr in c.params)
    kids, off = b"", sum(p[2] * p[3] for p in c.params)
    for ch in c.children:
        kids += _desc(ch, off)
        off += _datasize(ch) * ch.arraysize
    head = b"COL0" + c.name.encode().ljust(24, b"\0") + struct.pack(
        "<5I", len(prs), len(kids), offset, _datasize(c), c.arraysize)
    return head + prs + kids


def build_dump(root: C, values: dict[str, int | str] | None = None, md5: str | None = None) -> bytes:
    """An MMSXLIT blob for ``root``; ``values`` by name path ("InputChannel:3/PEQ/On")."""
    desc = _desc(root, 0)
    data = bytearray(_datasize(root) * root.arraysize)
    md5 = md5 or hashlib.md5(desc).hexdigest()
    head = b"MMSXLIT\0" + root.name.encode().ljust(36, b"\0") + md5.encode().ljust(36, b"\0") \
        + struct.pack("<II", len(desc), len(data))
    blob = head + desc + bytes(data)
    if values:
        d = Dump(blob)
        for path, v in values.items():
            off, prm = d.locate(path)
            if prm.type == STR:
                raw = str(v).encode()[:prm.elemsize - 1].ljust(prm.elemsize, b"\0")
            else:
                raw = int(v).to_bytes(prm.elemsize, "little", signed=prm.type == INT)
            data[off:off + prm.elemsize] = raw
        blob = head + desc + bytes(data)
    return blob


def _band(name: str, arr: int = 1, switch: str | None = None) -> C:
    ps = [_p("Type", UINT, 1)] + ([_p(switch, UINT, 1)] if switch else []) + \
        [_p("Frequency", UINT, 4), _p("Gain", INT, 2), _p("Q", UINT, 2)]
    return C(name, arr, ps)


def _peq(hpf: bool, mids: int, low_switch: str | None, high_switch: str | None) -> C:
    bank = [C("OneKnob", 1, [_p("On", UINT, 1), _p("Mode", STR, 12), _p("Depth", UINT, 1)])]
    if hpf:
        bank.append(C("HPF", 1, [_p("On", UINT, 1), _p("Frequency", UINT, 4), _p("Slope", UINT, 1)]))
    bank.append(_band("BandL", switch=low_switch))
    if mids:
        bank.append(_band("Band", mids))
    bank.append(_band("BandH", switch=high_switch))
    backup = C("OneKnobBackup", 1, [], [b for b in bank[1:]])   # same bands, after Bank (as on the TF5)
    return C("PEQ", 1, [_p("On", UINT, 1)], [C("Bank", 1, [_p("Type", STR, 8)], bank), backup])


# InputChannel's collections on the TF5, in order: PEQ is 7 and Fader 19 on the wire.
INPUT_CHANNEL_CHILDREN = ["Label", "Patch", "Signal", "BuiltIn", "Slot", "Input", "DirectOut", "PEQ", "Gate", "Comp",
                          "AutoMixer", "ToMix", "ToStMix", "ToFX", "ToMono", "ToStereo", "SubGroup", "DCAGroup",
                          "MuteGroup", "Fader", "Library"]


def mixing_tree() -> C:
    """The shape of the TF5's Mixing function (subset), same parameter names and widths, and
    InputChannel's collections in the TF5's order so real wire addresses resolve."""
    label = C("Label", 1, [_p("Name", STR, 64), _p("Color", STR, 8)])
    fader = C("Fader", 1, [_p("Level", INT, 2), _p("On", UINT, 1)])
    real = {"Label": label, "Patch": C("Patch", 1, [_p("Port", STR, 16)]), "PEQ": _peq(True, 2, None, "LPFOn"),
            "Fader": fader}
    inputs = [real.get(n) or C(n, 1, [_p("Dummy", UINT, 1)]) for n in INPUT_CHANNEL_CHILDREN]
    return C("Mixing", 1, [], [
        C("InputChannel", 40, [], inputs),
        C("StInChannel", 4, [], [label, _peq(False, 0, "HPFOn", "LPFOn"), fader]),
        C("Mix", 8, [], [label, fader]),
        C("Stereo", 2, [], [label, _peq(False, 2, "HPFOn", "LPFOn"), fader]),
    ])


def default_mixing_values() -> dict[str, int | str]:
    """Ch1 as captured from the real console (guide section 5); a few others for variety."""
    v: dict[str, int | str] = {}
    for x in range(40):
        b = f"InputChannel:{x}/PEQ"
        v.update({f"InputChannel:{x}/Label/Name": f"ch{x + 1}", f"{b}/On": 1, f"{b}/Bank/Type": "BOLD",
                  f"{b}/Bank/HPF/Frequency": 800, f"{b}/Bank/HPF/Slope": 12,
                  f"{b}/Bank/BandL/Type": 1, f"{b}/Bank/BandL/Frequency": 1250, f"{b}/Bank/BandL/Q": 700,
                  f"{b}/Bank/Band:0/Frequency": 10000, f"{b}/Bank/Band:0/Q": 700,
                  f"{b}/Bank/Band:1/Frequency": 40000, f"{b}/Bank/Band:1/Q": 700,
                  f"{b}/Bank/BandH/Type": 1, f"{b}/Bank/BandH/Frequency": 100000, f"{b}/Bank/BandH/Q": 700})
    b = "InputChannel:0/PEQ/Bank"
    v.update({"InputChannel:0/Label/Name": "GRA", f"{b}/HPF/On": 1, f"{b}/HPF/Frequency": 2000,
              f"{b}/BandL/Type": 0, f"{b}/BandL/Frequency": 1600, f"{b}/BandL/Gain": -520, f"{b}/BandL/Q": 140,
              f"{b}/Band:0/Frequency": 8000, f"{b}/Band:0/Gain": -1410, f"{b}/Band:0/Q": 250,
              f"{b}/Band:1/Frequency": 33500, f"{b}/Band:1/Gain": -570, f"{b}/Band:1/Q": 350,
              f"{b}/BandH/Type": 1, f"{b}/BandH/Frequency": 100000, f"{b}/BandH/Gain": 460, f"{b}/BandH/Q": 700})
    b = "InputChannel:1/PEQ"
    v.update({"InputChannel:1/Label/Name": "Pastor", f"{b}/On": 0, f"{b}/Bank/Band:0/Gain": 600,
              f"{b}/Bank/OneKnob/On": 1})
    for x in range(2):
        b = f"Stereo:{x}/PEQ"
        v.update({f"Stereo:{x}/Label/Name": "Stereo", f"{b}/On": 1, f"{b}/Bank/Type": "BOLD",
                  f"{b}/Bank/BandL/Frequency": 1250, f"{b}/Bank/BandL/Q": 700,
                  f"{b}/Bank/Band:0/Frequency": 10000, f"{b}/Bank/Band:0/Q": 700,
                  f"{b}/Bank/Band:1/Frequency": 40000, f"{b}/Bank/Band:1/Q": 700,
                  f"{b}/Bank/BandH/Frequency": 100000, f"{b}/Bank/BandH/Q": 700})
    v["Stereo:1/PEQ/Bank/BandL/Type"] = 2      # the low band as a high-pass filter...
    v["Stereo:1/PEQ/Bank/BandL/HPFOn"] = 1     # ...switched on
    v["Stereo:1/PEQ/Bank/BandL/Frequency"] = 800
    v["Stereo:1/PEQ/Bank/Band:1/Gain"] = -300
    return v


def property_tree() -> C:
    return C("Property", 1, [], [
        C("Specification", 1, [], [C("Unit", 1, [_p("Model", STR, 16)])]),
        C("Status", 1, [], [C("CommSession", 4, [_p("ClientType", STR, 16), _p("State", UINT, 4)])]),
        C("UserAccount", 1, [], [C("Unit", 1, [_p("Password", STR, 32)])]),
    ])


def wire_address(dump: Dump, path: str) -> tuple[list[int], list[int]]:
    """Name path -> (wire path, index per level): the inverse of Dump.resolve."""
    wp, idx, _ = dump.address(path)
    return wp, idx


def set_param(func: str, path: list[int], idx: list[int], value: int | str, origin: int = 0xA1,
              vtype: int | None = None) -> bytes:
    """A console -> client set-param notification (built only by the FAKE console)."""
    if isinstance(value, str):
        v = element(T_STR, value)
    else:
        v = element(vtype or (T_S32 if value < 0 else T_U32), [value])
    return message(SET_PARAM, [element(T_U8, b"\0"), element(T_STR, func), element(T_STR, func),
                               element(T_U16, [len(path)]), element(T_U32, path), element(T_U32, idx),
                               element(T_U32, [origin]), v])


def dump_message(func: str, blob: bytes) -> bytes:
    return message(DUMP, [element(T_U8, b"\0"), element(T_STR, func), element(T_STR, func),
                          element(T_U16, [0]), element(T_U32, []), element(T_U32, []), element(T_U32, [240]),
                          element(T_U8, blob), element(T_U8, b"")])


def console_hash(func: str, md5: str | None = None) -> bytes:
    return message(HASH_COMPARE, [element(T_U8, b"\0"), element(T_STR, func),
                                  element(T_U8, bytes.fromhex(md5 or DESC_MD5[func])),
                                  element(T_U8, hashlib.md5(func.encode()).digest())])


# ---------------------------------------------------------------------- fake console

class SimEditor:
    """Console side of TCP 50368. Records everything the client sends in ``received``."""

    def __init__(self, mixing_values: dict[str, int | str] | None = None, *, send_on_set_client_type: bool = True,
                 send_mixing_dump: bool = True, mixing_md5: str | None = None, echo_writes: bool = True,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.echo_writes = echo_writes   # like the TF5: apply a client's set-param and echo it back
        self.mixing_values = default_mixing_values() if mixing_values is None else mixing_values
        self.send_on_set_client_type = send_on_set_client_type
        self.send_mixing_dump = send_mixing_dump
        self.mixing_md5 = mixing_md5 or DESC_MD5["Mixing"]
        self.mixing_blob = build_dump(mixing_tree(), self.mixing_values, md5=self.mixing_md5)
        self.mixing = Dump(self.mixing_blob)   # for computing wire addresses
        self.property_blob = build_dump(property_tree(), {
            "Specification/Unit/Model": "TF5", "Status/CommSession:0/ClientType": "Editor",
            "UserAccount/Unit/Password": PASSWORD}, md5=DESC_MD5["Property"])
        self.received: list[tuple[str, Message]] = []
        self.writes: list[tuple[str, int]] = []   # (name path, value) of every set-param the client sent
        self.raw: list[tuple[str, bytes]] = []
        self.srv = socket.create_server((host, port))
        self.port = self.srv.getsockname()[1]
        self.conns: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    # -------------------------------------------------- server

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        reader = FrameReader()
        try:
            while chunk := conn.recv(65536):
                for tag, payload in reader.feed(chunk):
                    msg = parse_message(payload)
                    with self._lock:
                        self.received.append((tag, msg))
                        self.raw.append((tag, payload))
                    self._respond(conn, tag, msg)
        except OSError:
            pass

    def _send(self, conn: socket.socket, tag: str, payload: bytes) -> None:
        conn.sendall(frame(tag, payload))

    def _respond(self, conn: socket.socket, tag: str, msg: Message) -> None:
        if tag == "MPRO" and msg.cmd == HASH_COMPARE:
            self._send(conn, "MPRO", console_hash("Property"))
            self._send(conn, "MPRO", message(SYNC_OPEN))
            self._send(conn, "MPRO", dump_message("Property", self.property_blob))
            self._send(conn, "MPRO", message(SYNC_CLOSE))
        elif tag == "MMIX" and msg.cmd == HASH_COMPARE:
            self._send(conn, "MMIX", console_hash("Mixing", self.mixing_md5))
            if self.send_mixing_dump:
                self._send(conn, "MMIX", message(SYNC_OPEN))
                self._send(conn, "MMIX", dump_message("Mixing", self.mixing_blob))
                self._send(conn, "MMIX", message(SYNC_CLOSE))
        elif tag == "MMIX" and msg.cmd == SET_PARAM:
            e = msg.elements
            name, _, _ = self.mixing.resolve(e[4].value, e[5].value)
            with self._lock:
                self.writes.append((name, e[7].value[0]))
                self.mixing.apply(e[4].value, e[5].value, e[7].value[0])
            if self.echo_writes:
                self._send(conn, "MMIX", set_param("Mixing", e[4].value, e[5].value, e[7].value[0],
                                                   origin=0xA0, vtype=e[7].type))
        elif tag == "EEVT" and msg.cmd == EVENT:
            name, args = msg.elements[2].value, msg.elements[3].value
            self._send(conn, "EEVT", event(name, args, reply=True))
            if name == "SetClientType":
                prop = Dump(self.property_blob)
                wp, idx = wire_address(prop, "Status/CommSession:1/ClientType")
                self._send(conn, "MPRO", set_param("Property", wp, idx, args.split()[0][5:]))
                if self.send_on_set_client_type:
                    self._send(conn, "EEVT", event("onSetClientType"))

    # -------------------------------------------------- test controls

    def push(self, tag: str, payload: bytes) -> None:
        for c in list(self.conns):
            try:
                c.sendall(frame(tag, payload))
            except OSError:
                pass

    def push_raw(self, data: bytes) -> None:
        for c in list(self.conns):
            c.sendall(data)

    def push_change(self, path: str, value: int | str, **kw) -> None:
        wp, idx = wire_address(self.mixing, path)
        self.push("MMIX", set_param("Mixing", wp, idx, value, **kw))

    def push_event(self, name: str, args: str = "") -> None:
        self.push("EEVT", event(name, args))

    def push_dump(self, values: dict[str, int | str]) -> None:
        """A full Mixing dump with ``values`` over the defaults (e.g. after a scene recall)."""
        merged = {**self.mixing_values, **values}
        blob = build_dump(mixing_tree(), merged, md5=self.mixing_md5)
        self.push("MMIX", message(SYNC_OPEN))
        self.push("MMIX", dump_message("Mixing", blob))
        self.push("MMIX", message(SYNC_CLOSE))

    def sent_by_client(self) -> list[tuple[str, tuple[int, int, int]]]:
        with self._lock:
            return [(t, m.cmd) for t, m in self.received]

    def events_from_client(self) -> list[tuple[tuple[int, int, int], str, str]]:
        with self._lock:
            return [(m.cmd, m.elements[2].value, m.elements[3].value) for t, m in self.received if t == "EEVT"]

    def drop_connections(self) -> None:
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        self.conns.clear()

    def close(self) -> None:
        self.drop_connections()
        self.srv.close()


# ---------------------------------------------------------------------- RCP (TCP 49280)

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
    ("Pastor", "Yellow", 0, 1, 0), ("Lead Vox", "Red", -350, 1, 0), ("BGV 1", "Pink", -800, 1, -20),
    ("BGV 2", "Pink", -800, 1, 20), ("Host", "Yellow", None, 0, 0), ("Kick", "Blue", -500, 1, 0),
    ("Snare", "Blue", -700, 1, 0), ("OH L", "Blue", -1200, 1, -63), ("OH R", "Blue", -1200, 1, 63),
    ("Bass DI", "Green", -400, 1, 0), ("Gtr", "Orange", -900, 1, -25), ("Keys L", "Purple", -1000, 1, -63),
    ("Keys R", "Purple", -1000, 1, 63), ("Acoustic", "Orange", -1100, 1, 25), ("Video L", "SkyBlue", None, 0, -63),
    ("Video R", "SkyBlue", None, 0, 63),
]
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
        v.update({(inch + "Label/Name", x, 0): f'"{name}"', (inch + "Label/Color", x, 0): f'"{color}"',
                  (inch + "Fader/Level", x, 0): str(-32768 if level is None else level),
                  (inch + "Fader/On", x, 0): str(on), (inch + "ToStereo/Pan", x, 0): str(pan),
                  (inch + "ToMono/Level", x, 0): "-1000"})
        for m in range(len(DEMO_MIXES)):   # every source in every monitor mix, most turned down
            v[(inch + "ToMix/Level", x, m)] = str(-600 if (x + m) % 3 == 0 else -32768)
    v[(inch + "ToFx/Level", 1, 0)] = "-1200"   # reverb on the lead vocal
    for x, name in enumerate(["ST L", "ST R"]):
        v[(RCP_PREFIX["St"] + "Label/Name", x, 0)] = f'"{name}"'
        v[(RCP_PREFIX["St"] + "Fader/Level", x, 0)] = "0"
    for m in range(20):
        v[(RCP_PREFIX["Mix"] + "Label/Name", m, 0)] = f'"{DEMO_MIXES[m] if m < len(DEMO_MIXES) else f"Mix {m + 1}"}"'
    return v


class SimRCP:
    """Console side of TCP 49280: ``prminfo`` / ``get`` replies; ``NOTIFY set`` on desk changes.

    ``drift_s`` > 0 moves a random fader of the named inputs by a few dB every that
    many seconds, so open pages visibly follow "the desk". ``values`` (raw reply
    tokens by (address, x, y)) and ``prminfo`` (the parameter list's entries)
    default to the demo console's. ``received`` keeps the latest lines clients sent.
    """

    def __init__(self, values: dict[tuple[str, int, int], str] | None = None, *, prminfo: list[str] | None = None,
                 host: str = "127.0.0.1", port: int = 0, drift_s: float = 0.0, seed: int = 1) -> None:
        self.values = demo_rcp_values() if values is None else values
        self.prminfo = prminfo if prminfo is not None else [
            f'"{RCP_PREFIX[k]}{p}" {xs} {ys if ys > 1 else 0} {lo} {hi} {d} "{u}" {t} any rw {s}'
            for k, p, xs, ys, lo, hi, d, u, t, s in RCP_PARAMS]
        self.requests = 0
        self.received: deque[str] = deque(maxlen=100_000)
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
                out = [self.reply(raw.decode(errors="replace").strip()) for raw in lines]
                conn.sendall("".join(o + "\n" for o in out if o).encode())
        except OSError:
            pass
        finally:
            if conn in self.conns:
                self.conns.remove(conn)

    def reply(self, line: str) -> str:
        parts = line.split()
        if not parts:
            return ""
        self.requests += 1
        self.received.append(line)
        if parts[0] == "prminfo" and len(parts) == 2 and parts[1].isdigit():
            i = int(parts[1])
            return f"OK prminfo {i} {self.prminfo[i]}" if i < len(self.prminfo) else f"ERROR prminfo {i} UnknownIndex"
        if parts[0] == "get" and len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            with self._lock:
                value = self.values.get((parts[1], int(parts[2]), int(parts[3])))
            if value is None:
                return f"ERROR get {parts[1]} InvalidArgument"
            return f"OK get {parts[1]} {parts[2]} {parts[3]} {value}"
        return f"ERROR {parts[0]} UnknownCommand"   # tf-daemon never sends set; refuse anything else

    def set(self, addr: str, x: int, y: int, value: str) -> None:
        """Change a value "on the desk": stored and pushed to every client."""
        with self._lock:
            self.values[(addr, x, y)] = value
        self.push(f"NOTIFY set {addr} {x} {y} {value}")

    def push(self, line: str) -> None:
        for c in list(self.conns):
            try:
                c.sendall((line + "\n").encode())
            except OSError:
                pass

    def _drift(self, period: float) -> None:
        addr = RCP_PREFIX["InCh"] + "Fader/Level"
        while not self._stop.wait(period):
            x = self._rng.randrange(len(DEMO_INPUTS))
            with self._lock:
                level = int(self.values[(addr, x, 0)])
            if level > -32768:
                self.set(addr, x, 0, str(max(-4000, min(1000, level + self._rng.choice([-300, -150, 150, 300])))))

    def drop_connections(self) -> None:
        """Close every client connection, as a console reboot or cable pull would."""
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR)   # close() alone may not send a FIN while _serve is in recv
            except OSError:
                pass
            c.close()

    def close(self) -> None:
        self._stop.set()
        self.drop_connections()
        self.srv.close()


# ---------------------------------------------------------------------- both protocols

def demo_mixing_values() -> dict[str, int | str]:
    """The editor protocol's view of the demo desk: the test defaults (ch1 has the EQ captured
    from a real console) with the demo channel names."""
    v = default_mixing_values()
    for x, (name, *_) in enumerate(DEMO_INPUTS):
        v[f"InputChannel:{x}/Label/Name"] = name
    return v


class SimConsole:
    """A simulated TF5: RCP and the TF Editor protocol on one host.

    Port 0 picks free ports (``rcp_port`` / ``eq_port`` say which); use the real
    49280 / 50368 to stand in for a console at ``tf5.host``.
    """

    def __init__(self, host: str = "127.0.0.1", rcp_port: int = 0, eq_port: int = 0, drift_s: float = 4.0) -> None:
        self.host = host
        self.rcp = SimRCP(host=host, port=rcp_port, drift_s=drift_s)
        self.editor = SimEditor(demo_mixing_values(), host=host, port=eq_port)
        self.rcp_port, self.eq_port = self.rcp.port, self.editor.port
        log.info("Simulated TF5 on %s: RCP port %d, TF Editor port %d", host, self.rcp_port, self.eq_port)

    def close(self) -> None:
        self.rcp.close()
        self.editor.close()

    def status(self) -> dict:
        return {"host": self.host, "rcp_port": self.rcp_port, "eq_port": self.eq_port,
                "rcp_requests": self.rcp.requests, "eq_writes": len(self.editor.writes)}

    # The desk as the simulated stage hears it (audio.stage.DeskMix).

    def channel_eq(self, x: int) -> dict[str, Any] | None:
        with self.editor._lock:
            return channel_eq(self.editor.mixing, "InCh", x)

    def fader_db(self, x: int) -> float | None:
        return self._fader("InCh", x)

    def master_db(self) -> float:
        db = self._fader("St", 0)
        return -138.0 if db is None else db

    def _fader(self, kind: str, x: int) -> float | None:
        values, prefix = self.rcp.values, RCP_PREFIX[kind]
        with self.rcp._lock:
            on = values.get((prefix + "Fader/On", x, 0), "1")
            level = int(values.get((prefix + "Fader/Level", x, 0), "0"))
        return None if on == "0" or level <= -32768 else level / 100


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tf_daemon.console.sim",
                                 description="Simulated TF5: point tf5.host (with tf5.eq / eq_write) at it.")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 for other machines)")
    ap.add_argument("--rcp-port", type=int, default=49280)
    ap.add_argument("--eq-port", type=int, default=50368)
    ap.add_argument("--drift", type=float, default=4.0, help="seconds between simulated fader moves (0 = off)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sim = SimConsole(args.host, args.rcp_port, args.eq_port, args.drift)
    try:
        last = 0
        while True:
            time.sleep(1)
            if len(sim.editor.writes) != last:
                for path, value in sim.editor.writes[last:]:
                    log.info("EQ write from the client: %s = %s", path, value)
                last = len(sim.editor.writes)
    except KeyboardInterrupt:
        return 0
    finally:
        sim.close()


if __name__ == "__main__":
    raise SystemExit(main())
