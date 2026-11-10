"""Simulated TF5, the TF Editor side (console/editor): the console side of the handshake
recorded from TF Editor (guides/tf5_api.md, section 6), serving all 10 functions of the V4.55
tree on their own frame tags (subscribe, hash compare, dump, set-param echo, pushes), with
dumps built by sim_tree.py from the real tree so real wire addresses resolve. It applies and
echoes set-params like the TF5, so writes work against it, answers ``RecallScene`` with
``onRecalledScene`` (and updates ``Setup/Scene/LastScene``; a Mixing write sets
``Scene/EditFlag``), and records every frame the client
sends so tests can check that nothing but read-only handshake traffic left the client.
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import threading
from collections.abc import Callable, Iterable
from typing import Any

from .editor.codec import (
    DESC_MD5,
    EVENT,
    HASH_COMPARE,
    SET_PARAM,
    SYNC_CLOSE,
    SYNC_OPEN,
    T_STR,
    FrameReader,
    Message,
    event,
    frame,
    message,
    parse_message,
)
from .editor.dump import Dump
from .editor.tree import FUNCTIONS, TAGS, function_of_tag
from .sim_tree import (
    MOBILE_PASSWORD,
    PASSWORD,
    C,
    build_dump,
    console_hash,
    default_mixing_values,
    default_values,
    dump_message,
    function_dump,
    function_tree,
    mixing_tree,
    property_tree,
    set_param,
    wire_address,
)

log = logging.getLogger(__name__)

RECALL_STATUS = "OK"  # onRecalledScene's STATUS on success (the real desk's token is PENDING)

__all__ = [  # the builders live in sim_tree.py; re-exported for the tests and console/sim.py
    "C",
    "MOBILE_PASSWORD",
    "PASSWORD",
    "SimEditor",
    "build_dump",
    "console_hash",
    "default_mixing_values",
    "default_values",
    "dump_message",
    "function_dump",
    "function_tree",
    "mixing_tree",
    "property_tree",
    "set_param",
    "wire_address",
]


class SimEditor:
    """Console side of TCP 50368. Records everything the client sends in ``received``.

    ``mixing_values``: the Mixing data (default: default_mixing_values()); ``values``: per other
    function, values over its demo values (sim_tree.default_values). ``functions``: which it
    serves (default all 10); a subscribe to another gets no answer.
    """

    def __init__(
        self,
        mixing_values: dict[str, int | str] | None = None,
        *,
        values: dict[str, dict[str, int | str]] | None = None,
        functions: Iterable[str] = FUNCTIONS,
        send_on_set_client_type: bool = True,
        send_mixing_dump: bool = True,
        mixing_md5: str | None = None,
        echo_writes: bool = True,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.echo_writes = echo_writes  # like the TF5: apply a client's set-param and echo it back
        self.mixing_values = default_mixing_values() if mixing_values is None else mixing_values
        self.send_on_set_client_type = send_on_set_client_type
        self.send_mixing_dump = send_mixing_dump
        self.mixing_md5 = mixing_md5 or DESC_MD5["Mixing"]
        self.dumps: dict[str, Dump] = {}  # function -> the desk's live data
        self._heads: dict[str, bytes] = {}  # function -> MMSXLIT header + descriptor
        served = set(functions) | {"Property", "Mixing"}
        for func in (f for f in FUNCTIONS if f in served):
            v = self.mixing_values if func == "Mixing" else {**default_values(func), **(values or {}).get(func, {})}
            self._load(func, function_dump(func, v, self.mixing_md5 if func == "Mixing" else None))
        self.mixing_blob = self.blob("Mixing")
        self.property_blob = self.blob("Property")
        self.received: list[tuple[str, Message]] = []
        self.writes: list[tuple[str, int | str]] = []  # (name path, value) of every Mixing set-param the client sent
        self.all_writes: list[tuple[str, str, int | str]] = []  # (function, name path, value), every function
        self.recalls: list[str] = []  # RecallScene NUMBERs the client sent
        self.on_write: Callable[[str, int], None] | None = None  # after a client's Mixing set-param is applied
        self.on_recall: Callable[[str], None] | None = None  # after a client's recall, with its NUMBER ("A05")
        self.raw: list[tuple[str, bytes]] = []
        self.srv = socket.create_server((host, port))
        self.port = self.srv.getsockname()[1]
        self.conns: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _load(self, func: str, blob: bytes) -> None:
        desc_len = struct.unpack_from("<I", blob, 0x50)[0]
        self._heads[func] = blob[: 0x58 + desc_len]
        self.dumps[func] = Dump(blob)

    @property
    def mixing(self) -> Dump:
        return self.dumps["Mixing"]

    def blob(self, func: str) -> bytes:
        """The MMSXLIT dump of ``func`` with the desk's current values."""
        return self._heads[func] + bytes(self.dumps[func].data)

    # -------------------------------------------------- server

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
        except (ValueError, KeyError) as e:  # a malformed frame or unknown parameter: drop the client
            log.debug("Simulated editor console dropped a client: %s", e)
            conn.close()

    def _send(self, conn: socket.socket, tag: str, payload: bytes) -> None:
        conn.sendall(frame(tag, payload))

    def _respond(self, conn: socket.socket, tag: str, msg: Message) -> None:
        func = function_of_tag(tag)
        if func in self.dumps and msg.cmd == HASH_COMPARE:
            self._send(conn, tag, console_hash(func, self.mixing_md5 if func == "Mixing" else None))
            if func != "Mixing" or self.send_mixing_dump:
                with self._lock:
                    blob = self.blob(func)
                self._send(conn, tag, message(SYNC_OPEN))
                self._send(conn, tag, dump_message(func, blob))
                self._send(conn, tag, message(SYNC_CLOSE))
        elif func in self.dumps and func != "Property" and msg.cmd == SET_PARAM:
            self._set(conn, func, msg)
        elif tag == "EEVT" and msg.cmd == EVENT:
            name, args = msg.elements[2].value, msg.elements[3].value
            self._send(conn, "EEVT", event(name, args, reply=True))
            if name == "SetClientType":
                wp, idx = wire_address(self.dumps["Property"], "Status/CommSession:1/ClientType")
                self._send(conn, "MPRO", set_param("Property", wp, idx, args.split()[0][5:]))
                if self.send_on_set_client_type:
                    self._send(conn, "EEVT", event("onSetClientType"))
            elif name == "RecallScene":
                self._recall(conn, args)

    def _set(self, conn: socket.socket, func: str, msg: Message) -> None:
        e = msg.elements
        value: Any = e[7].value if e[7].type == T_STR else e[7].value[0]
        dump = self.dumps[func]
        name, _, _ = dump.resolve(e[4].value, e[5].value)
        with self._lock:
            dump.apply(e[4].value, e[5].value, value)
            self.all_writes.append((func, name, value))
            if func == "Mixing":
                self.writes.append((name, value))
        if func == "Mixing" and self.on_write is not None:
            self.on_write(name, value)
        if self.echo_writes:
            self._send(conn, TAGS[func], set_param(func, e[4].value, e[5].value, value, origin=0xA0, vtype=e[7].type))
        if func == "Mixing" and "Setup" in self.dumps and self.dumps["Setup"].get("Scene/EditFlag") == 0:
            self.push_change("Scene/EditFlag", 1, func="Setup")  # the scene is now edited [INFERRED]

    def _recall(self, conn: socket.socket, args: str) -> None:
        """Like the TF5: recall, report the scene now loaded, and announce it."""
        number = dict(t.partition(":")[::2] for t in args.split()).get("NUMBER", "")
        with self._lock:
            self.recalls.append(number)
        ok = re.fullmatch(r"[AB]\d{2,3}", number) is not None and int(number[1:]) <= 99
        if ok and "Setup" in self.dumps:
            self.push_change("Scene/LastScene", number, func="Setup")
            self.push_change("Scene/EditFlag", 0, func="Setup")
        self._send(conn, "EEVT", event("onRecalledScene", f"STATUS:{RECALL_STATUS if ok else 'Error'} NUMBER:{number}"))
        if ok and self.on_recall is not None:
            self.on_recall(number)

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

    def push_change(self, path: str, value: int | str, func: str = "Mixing", **kw: Any) -> None:
        """A change "on the desk": applied to the desk's data and pushed to every client."""
        dump = self.dumps[func]
        wp, idx = wire_address(dump, path)
        with self._lock:
            dump.apply(wp, idx, value)
        self.push(TAGS[func], set_param(func, wp, idx, value, **kw))

    def push_event(self, name: str, args: str = "") -> None:
        self.push("EEVT", event(name, args))

    def push_dump(self, values: dict[str, int | str], func: str = "Mixing") -> None:
        """A full dump of ``func`` with ``values`` over the defaults (e.g. after a scene recall)."""
        base = self.mixing_values if func == "Mixing" else default_values(func)
        blob = function_dump(func, {**base, **values}, self.mixing_md5 if func == "Mixing" else None)
        with self._lock:
            self._load(func, blob)
        tag = TAGS[func]
        self.push(tag, message(SYNC_OPEN))
        self.push(tag, dump_message(func, blob))
        self.push(tag, message(SYNC_CLOSE))

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
