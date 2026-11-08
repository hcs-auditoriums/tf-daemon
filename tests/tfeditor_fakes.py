"""Test doubles for the TF Editor protocol: synthetic MMSXLIT dumps and a fake console.

The fake console speaks the console side of the handshake recorded from TF
Editor (guides/tf5_api.md, section 6). It records every frame the client
sends so tests can check that nothing but read-only handshake traffic left
the client.
"""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from tf_daemon.tfeditor import (DESC_MD5, DUMP, EVENT, EVENT_REPLY, HASH_COMPARE, SET_PARAM, SUBSCRIBE,
                                SYNC_CLOSE, SYNC_OPEN, T_S32, T_STR, T_U8, T_U16, T_U32, Dump, FrameReader,
                                Message, element, event, frame, message, parse_message)

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

class FakeEditorConsole:
    """Console side of TCP 50368. Records everything the client sends in ``received``."""

    def __init__(self, mixing_values: dict[str, int | str] | None = None, *, send_on_set_client_type: bool = True,
                 send_mixing_dump: bool = True, mixing_md5: str | None = None, echo_writes: bool = True) -> None:
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
        self.srv = socket.create_server(("127.0.0.1", 0))
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


def wait_until(pred, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


__all__ = ["C", "FakeEditorConsole", "PASSWORD", "build_dump", "console_hash", "default_mixing_values",
           "dump_message", "mixing_tree", "property_tree", "set_param", "wait_until", "wire_address",
           "SUBSCRIBE", "EVENT_REPLY"]
