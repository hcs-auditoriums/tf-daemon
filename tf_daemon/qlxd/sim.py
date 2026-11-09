"""Simulated QLXD4 receivers (demo mode and tests).

Each SimReceiver is a TCP server on 127.0.0.1 that answers GET requests the
way a QLXD4 does, with a draining battery and a wandering RF level. It also
pushes an unsolicited REP when the battery changes, like real hardware.
"""

from __future__ import annotations

import logging
import math
import random
import re
import socket
import threading
import time

from . import protocol as p

log = logging.getLogger(__name__)

_GET = re.compile(r"GET\s+(?:(\d+)\s+)?(\S+)")


class SimReceiver:
    def __init__(
        self,
        name: str,
        freq_khz: int,
        group_chan: str = "1,1",
        batt_type: str = "LION",
        run_min: float = 300.0,
        tx: str | None = "QLXD2_SM58",
        rf_base: float = -55.0,
        seed: int = 0,
    ) -> None:
        self.name, self.freq_khz, self.group_chan = name, freq_khz, group_chan
        self.batt_type, self.run_min, self.tx = batt_type, run_min, tx
        self.rf_base = rf_base
        self._rng = random.Random(seed)
        self.mac = f"00:0E:DD:00:00:{seed:02X}"
        self._t0 = time.monotonic()
        self._srv = socket.create_server(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []

    # ------------------------------------------------------------------ values

    def _runtime(self) -> int:
        # Drains at 60x real time so the demo visibly changes.
        return max(0, int(self.run_min - (time.monotonic() - self._t0)))

    def values(self) -> dict[str, str]:
        has_tx = self.tx is not None
        run = self._runtime()
        charge = round(100 * run / 600)
        t = time.monotonic() - self._t0
        rf = self.rf_base + 6 * math.sin(t / 7) + self._rng.uniform(-3, 3)
        audio = -25 + 12 * abs(math.sin(t * 1.7)) + self._rng.uniform(-4, 4)
        return {
            "DEVICE_ID": "{" + f"{self.name[:8]:<8}" + "}",
            "MODEL": "{QLXD4                           }",
            "FW_VER": "{2.3.6.0  }",
            "MAC_ADDR": self.mac,
            "CHAN_NAME": "{" + f"{self.name:<31}" + "}",
            "FREQUENCY": f"{self.freq_khz:06d}",
            "GROUP_CHAN": "{" + f"{self.group_chan:<5}" + "}",
            "AUDIO_GAIN": "018",
            "RF_INT_DET": "NONE",
            "TX_TYPE": self.tx or "UNKN",
            "TX_RF_PWR": "NORMAL" if has_tx else "UNKN",
            "TX_MUTE_STATUS": "OFF",
            "BATT_TYPE": self.batt_type if has_tx else "UNKN",
            "BATT_BARS": f"{min(5, math.ceil(charge / 20)):03d}" if has_tx else "255",
            "BATT_CHARGE": f"{charge:03d}" if has_tx and self.batt_type == "LION" else "255",
            "BATT_RUN_TIME": f"{run:05d}" if has_tx else "65535",
            "RX_RF_LVL": f"{max(0, min(115, round(rf + p.RF_OFFSET_DB))):03d}" if has_tx else "000",
            "RF_ANTENNA": ("AX" if math.sin(t / 3) > 0 else "XB") if has_tx else "XX",
            "AUDIO_LVL": f"{max(0, min(50, round(audio + p.AUDIO_OFFSET_DB))):03d}" if has_tx else "000",
        }

    # ------------------------------------------------------------------ server

    def start(self) -> None:
        threading.Thread(target=self._accept, name=f"sim-{self.port}", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._srv.close()
        for c in list(self._clients):
            try:
                c.close()
            except OSError:
                pass

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        reader = p.MessageReader()
        conn.settimeout(0.5)
        last_bars = None
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    data = None
                if data == b"":
                    return
                vals = self.values()
                out = []
                for body in reader.feed(data or b""):
                    m = _GET.match(body)
                    if not m:
                        out.append("< REP ERR >")
                        continue
                    ch, param = m.groups()
                    if param == "ALL":  # like real QLXD4s: includes the device-level values, not MODEL
                        out += [_rep(k, v) for k, v in vals.items() if k != "MODEL"]
                    elif param in vals:
                        out.append(_rep(param, vals[param]))
                    else:
                        out.append("< REP ERR >")
                if vals["BATT_BARS"] != last_bars:  # unsolicited change report
                    if last_bars is not None:
                        out.append(_rep("BATT_BARS", vals["BATT_BARS"]))
                    last_bars = vals["BATT_BARS"]
                if out:
                    conn.sendall("".join(out).encode())
        except OSError:
            pass
        finally:
            conn.close()


def _rep(param: str, value: str) -> str:
    if param in ("DEVICE_ID", "MODEL", "FW_VER", "MAC_ADDR"):
        return f"< REP {param} {value} >"
    return f"< REP 1 {param} {value} >"


def demo_receivers() -> list[SimReceiver]:
    return [
        SimReceiver("Lead Vox", 578350, "1,1", "LION", run_min=420, rf_base=-48, seed=1),
        SimReceiver("BGV 1", 584125, "1,2", "LION", run_min=95, rf_base=-62, seed=2),
        SimReceiver("Pastor HH", 590600, "1,3", "ALKA", run_min=25, tx="QLXD2_B58", rf_base=-70, seed=3),
        SimReceiver("Spare", 596875, "1,4", tx=None, seed=4),
    ]
