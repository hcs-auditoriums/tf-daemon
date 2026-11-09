"""``python -m tf_daemon.console.editor HOST``: print a TF5's EQ over the TF Editor protocol (read-only)."""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Any

from .client import DEFAULT_PORT, SYNC_TIMEOUT_S, TFEditorClient


def _parse_channels(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def main(argv: list[str] | None = None) -> int:
    """Connect once, print the EQ of some input channels, optionally keep watching. Read-only."""
    ap = argparse.ArgumentParser(
        prog="python -m tf_daemon.console.editor",
        description="Read (never write) the TF5's EQ over the TF Editor protocol.",
    )
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--channels", default="1-8", help="input channels, e.g. 1-8,12 (default 1-8)")
    ap.add_argument("--watch", action="store_true", help="keep running and print changes")
    ap.add_argument("--timeout", type=float, default=SYNC_TIMEOUT_S + 5)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    chans = _parse_channels(args.channels)
    changed = threading.Event()
    client = TFEditorClient(args.host, args.port, on_change=changed.set)
    client.start()
    try:
        deadline = time.monotonic() + args.timeout
        while not client.synced and time.monotonic() < deadline:
            time.sleep(0.1)
        if not client.synced:
            print(f"not synchronized: {client.status()}")
            return 1
        print(f"descriptor {client.desc_md5} (known: {client.status()['descriptor_known']})")
        last: dict[int, Any] = {}
        while True:
            for ch in chans:
                eq = client.eq("InCh", ch - 1)
                if eq != last.get(ch):
                    print(f"InCh {ch:2d} {client.get(f'InputChannel:{ch - 1}/Label/Name')!r}: {_fmt_eq(eq)}")
                    last[ch] = eq
            if not args.watch:
                return 0
            changed.wait(1.0)
            changed.clear()
    except KeyboardInterrupt:
        return 0
    finally:
        client.stop()
        print("sent:", ", ".join(f"{t} {bytes(c).hex(' ')}" for t, c in client.sent))


def _fmt_eq(eq: dict[str, Any] | None) -> str:
    if eq is None:
        return "-"
    parts = ["on" if eq["on"] else "OFF", eq["bank"]] + (["1-knob"] if eq["one_knob"] else [])
    if eq["hpf"]:
        parts.append(f"HPF {'on' if eq['hpf']['on'] else 'off'} {eq['hpf']['freq']:g} Hz")
    for b in eq["bands"]:
        parts.append(f"{b['label']}:{b['type']} {b['freq']:g} Hz {b['gain']:+.1f} dB Q{b['q']:g}")
    return " | ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
