"""Shure QLX-D (QLXD4) network protocol: TCP port 2202, ASCII messages.

    request:   < GET 1 BATT_RUN_TIME >
    reply:     < REP 1 BATT_RUN_TIME 00185 >
    device:    < REP DEVICE_ID {QLXD4-Stage   } >      (no channel index)
    meters:    < SAMPLE 1 ALL AX 102 035 >             (antenna, RF, audio)

Strings come in braces, space padded. Receivers also send REP messages on their
own whenever a value changes. This module only ever sends GET (read-only).
"""

from __future__ import annotations

import re
from typing import Any

PORT = 2202

# Sentinel values from the Shure command strings documentation.
BATT_BARS_UNKNOWN = 255
BATT_CHARGE_UNKNOWN = 255
RUN_TIME_UNKNOWN = 65535
RUN_TIME_CALCULATING = 65534
RUN_TIME_WARNING = 65533  # "communication warning": runtime not available

RF_OFFSET_DB = 128  # RX_RF_LVL 000..115 -> dBm = value - 128
AUDIO_OFFSET_DB = 50  # AUDIO_LVL 000..050 -> dBFS = value - 50
GAIN_OFFSET_DB = 18  # AUDIO_GAIN 000..060 -> dB = value - 18

_MSG = re.compile(rb"<([^<>]*)>")


def get(param: str, channel: int | None = 1) -> bytes:
    """A GET request; ``channel=None`` for device-level parameters."""
    return f"< GET {param} >".encode() if channel is None else f"< GET {channel} {param} >".encode()


class MessageReader:
    """Splits a TCP byte stream into ``< ... >`` message bodies."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        out = []
        end = 0
        for m in _MSG.finditer(self._buf):
            out.append(m.group(1).decode("ascii", errors="replace").strip())
            end = m.end()
        self._buf = self._buf[end:]
        if len(self._buf) > 65536:  # garbage without a closing '>'
            self._buf = b""
        return out


def parse(body: str) -> tuple[str, int | None, str, list[str]] | None:
    """``"REP 1 BATT_BARS 004"`` -> ("REP", 1, "BATT_BARS", ["004"]).

    A braced string value is returned as a single stripped token.
    """
    m = re.match(r"(REP|SAMPLE|REPORT)\s+(?:(\d+)\s+)?(\S+)\s*(.*)$", body, re.S)
    if not m:
        return None
    kind, ch, param, rest = m.groups()
    rest = rest.strip()
    if rest.startswith("{"):
        args = [rest[1 : rest.rfind("}") if "}" in rest else None].strip()]
    else:
        args = rest.split()
    return ("REP" if kind == "REPORT" else kind), (int(ch) if ch else None), param, args


def _int(s: str) -> int | None:
    try:
        return int(s)
    except ValueError:
        return None


def decode(param: str, args: list[str]) -> dict[str, Any]:
    """Map one REP parameter to receiver state fields (unknown params -> {})."""
    v = args[0] if args else ""
    n = _int(v)
    match param:
        case "DEVICE_ID":
            return {"device_id": v}
        case "MODEL":
            return {"model": v}
        case "FW_VER":
            return {"fw_ver": v}
        case "MAC_ADDR":
            return {"mac": v.upper() or None}
        case "CHAN_NAME":
            return {"name": v}
        case "FREQUENCY":
            return {"frequency_mhz": n / 1000 if n else None}  # kHz
        case "GROUP_CHAN":
            return {"group_chan": None if "-" in v else v.replace(",", " / ")}
        case "AUDIO_GAIN":
            return {"audio_gain_db": None if n is None else n - GAIN_OFFSET_DB}
        case "RF_INT_DET":
            return {"interference": v not in ("NONE", "")}
        case "ENCRYPTION_WARNING":
            return {"encryption_warning": v == "ON"}
        case "TX_TYPE":
            return {"tx_type": None if v in ("UNKN", "") else v}
        case "TX_RF_PWR":
            return {"tx_rf_power": None if v in ("UNKN", "") else v}
        case "TX_MUTE_STATUS" | "TX_MUTE_BUTTON_STATUS":
            return {"tx_muted": v == "ON"}
        case "BATT_TYPE":
            return {"batt_type": None if v in ("UNKN", "") else v}
        case "BATT_BARS":
            return {"batt_bars": None if n is None or n == BATT_BARS_UNKNOWN else n}
        case "BATT_CHARGE":
            return {"batt_charge": None if n is None or n == BATT_CHARGE_UNKNOWN else n}
        case "BATT_RUN_TIME":
            if n is None or n in (RUN_TIME_UNKNOWN, RUN_TIME_WARNING):
                return {"batt_run_min": None, "batt_calculating": False}
            if n == RUN_TIME_CALCULATING:
                return {"batt_run_min": None, "batt_calculating": True}
            return {"batt_run_min": n, "batt_calculating": False}
        case "RX_RF_LVL":
            return {"rf_dbm": None if n is None else n - RF_OFFSET_DB}
        case "RF_ANTENNA":
            return _antenna(v)
        case "AUDIO_LVL":
            return {"audio_dbfs": None if n is None else n - AUDIO_OFFSET_DB}
        case "ALL" if len(args) >= 3:  # SAMPLE 1 ALL <antenna> <rf> <audio>
            rf, audio = _int(args[1]), _int(args[2])
            return {
                **_antenna(args[0]),
                "rf_dbm": None if rf is None else rf - RF_OFFSET_DB,
                "audio_dbfs": None if audio is None else audio - AUDIO_OFFSET_DB,
            }
    return {}


def _antenna(v: str) -> dict[str, Any]:
    # Two characters: A or X, then B or X ("AX" = antenna A active).
    v = v.upper()
    return {"ant_a": v[:1] == "A", "ant_b": v[1:2] == "B"}
