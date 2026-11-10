"""Simulated TF5 (demo mode, development and tests): both of its network protocols.

    python -m tf_daemon.console.sim                 # RCP on 49280, TF Editor on 50368
    python -m tf_daemon --demo                      # the whole app, this console in-process

SimConsole runs SimRCP (sim_rcp.py) and SimEditor (sim_editor.py) as one desk: a fader
moved on one side shows on the other, and the simulated stage hears its EQ and faders.
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from typing import Any

from .editor.dump import channel_eq
from .sim_editor import SimEditor, default_mixing_values
from .sim_rcp import DEMO_INPUTS, RCP_PREFIX, SimRCP

log = logging.getLogger(__name__)


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
        mixing = demo_mixing_values()
        inch = RCP_PREFIX["InCh"]
        for x in range(40):  # one desk: the editor protocol sees the faders RCP does
            mixing[f"InputChannel:{x}/Fader/Level"] = int(self.rcp.values.get((inch + "Fader/Level", x, 0), "-32768"))
            mixing[f"InputChannel:{x}/Fader/On"] = int(self.rcp.values.get((inch + "Fader/On", x, 0), "1"))
        self.editor = SimEditor(mixing, host=host, port=eq_port)
        self.editor.on_write = self._editor_wrote
        self.rcp.on_set = self._rcp_set
        self.rcp_port, self.eq_port = self.rcp.port, self.editor.port
        log.info("Simulated TF5 on %s: RCP port %d, TF Editor port %d", host, self.rcp_port, self.eq_port)

    def close(self) -> None:
        self.rcp.close()
        self.editor.close()

    _FADER = re.compile(r"InputChannel:(\d+)/Fader/(Level|On)")

    def _editor_wrote(self, path: str, value: int) -> None:
        """A client set a fader over the editor protocol: the desk's fader moves, RCP sees it."""
        m = self._FADER.fullmatch(path)
        if m:
            x = int(m.group(1))
            self.rcp.held.add(x)
            self.rcp.store(RCP_PREFIX["InCh"] + f"Fader/{m.group(2)}", x, 0, str(value))

    def _rcp_set(self, addr: str, x: int, y: int, value: str) -> None:
        """A fader moved "on the desk": editor-protocol clients see it too (origin: the console)."""
        for kind in ("Level", "On"):
            if addr == RCP_PREFIX["InCh"] + f"Fader/{kind}" and y == 0:
                self.editor.push_change(f"InputChannel:{x}/Fader/{kind}", int(value))  # applies it too

    def status(self) -> dict:
        return {
            "host": self.host,
            "rcp_port": self.rcp_port,
            "eq_port": self.eq_port,
            "rcp_requests": self.rcp.requests,
            "eq_writes": len(self.editor.writes),
        }

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
    ap = argparse.ArgumentParser(
        prog="python -m tf_daemon.console.sim",
        description="Simulated TF5: point tf5.host (with tf5.eq / eq_write) at it.",
    )
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
