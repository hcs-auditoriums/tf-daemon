"""The Desk page's reads: scenes, USB recorder, FX and delay, selected channel (all over the TF
Editor protocol; TF5Module registers these on its /api/tf5 blueprint).

Every reply carries ``version`` (the change counters of the functions it reads); a request
with ``?since=<that version>`` gets ``{"version", "unchanged": true}`` while nothing changed,
so the page can poll cheaply. All are 404 when the editor protocol is off (tf5.eq = false).

Nothing here changes the desk. The page's changes go through the existing routes, each needing
``"confirm": true`` and the matching switch: POST /api/tf5/event/recall_scene (tf5.scene_recall)
and POST /api/tf5/param (FX: tf5.fx_write, delay: tf5.processing_write).

Routes:
  GET /desk                    the Desk page (static/desk.html)
  GET /api/tf5/scenes          scenes.scenes(): stored scenes of both banks (?all=1: every slot),
                               the current one, what may be done (recall; store / undo unavailable)
  GET /api/tf5/recorder        recorder.recorder(): transport, now playing, playlist, setup (read-only)
  GET /api/tf5/fx              effects.fx(): FX 1/2 and insert FX, raw parameter values
  GET /api/tf5/delay           effects.delay(): ST IN and matrix delays
  GET /api/tf5/selected        the desk's selected channel (ConsoleSetup/Common/SelectedCH), decoded [inferred]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Flask, jsonify, request

from . import effects, recorder, scenes

if TYPE_CHECKING:
    from .editor.client import TFEditorClient

# The channel-ID rule seen on one desk (docs/desk-coverage-howto-complete.md 3.11) [INFERRED]:
# CH k = CH1_ID + (k - 1) * CH_STEP. Other strip types use other high bits (not decoded).
CH1_ID = 0x0C0316C9
CH_STEP = 0x00100000
INPUT_CHANNELS = 40


def channel_of(ident: int) -> int | None:
    """The input channel number (1-40) a channel ID names by the inferred rule, else None."""
    k, rest = divmod(ident - CH1_ID, CH_STEP)
    return k + 1 if rest == 0 and 0 <= k < INPUT_CHANNELS else None


def selected(ec: TFEditorClient) -> dict[str, Any]:
    """The desk's selected channel: the raw ID, and the input channel it names [inferred]."""
    raw = ec.get("Common/SelectedCH", "ConsoleSetup")
    ch = channel_of(raw) if isinstance(raw, int) else None
    name = ec.get(f"InputChannel:{ch - 1}/Label/Name") if ch else None
    return {
        "raw": raw,
        "hex": f"0x{raw:08X}" if isinstance(raw, int) else None,
        "channel": ch,
        "label": f"CH {ch}" if ch else None,
        "name": name if isinstance(name, str) else None,
        "inferred": True,
        "note": "decoded with the channel-ID rule seen on one desk; read-only (selecting from here isn't allowed)",
    }


# area -> (functions it reads, what builds the reply)
AREAS: dict[str, tuple[tuple[str, ...], Callable[[TFEditorClient], dict[str, Any]]]] = {
    "recorder": (("Status", "Setup"), recorder.recorder),
    "fx": (("FX", "Mixing"), effects.fx),
    "delay": (("Mixing",), effects.delay),
    "selected": (("ConsoleSetup", "Mixing"), selected),
}


def version(ec: TFEditorClient, funcs: tuple[str, ...]) -> str:
    """Changes whenever one of ``funcs`` changed (or the session reconnected, or a scene was recalled)."""
    recalled = ec.last_recalled["at"] if ec.last_recalled else 0
    return ".".join(str(v) for v in (ec.connects, *(ec.versions.get(f, 0) for f in funcs), int(recalled * 1000)))


def register(app: Flask, bp: Blueprint, client: Callable[[], TFEditorClient | None]) -> None:
    """Add the page and the read routes; ``client()`` is the editor-protocol client (None: off)."""
    app.add_url_rule("/desk", "desk_page", lambda: app.send_static_file("desk.html"))
    cache: dict[tuple[str, bool], tuple[str, dict[str, Any]]] = {}  # the last reply per area, by version

    def reply(area: str, funcs: tuple[str, ...], build: Callable[[TFEditorClient], dict[str, Any]], all_: bool = False):
        ec = client()
        if ec is None:
            return jsonify({"error": "tf5.eq is off"}), 404
        v = version(ec, funcs)
        if request.args.get("since") == v:
            return jsonify({"version": v, "unchanged": True})
        hit = cache.get((area, all_))
        if hit is None or hit[0] != v:
            hit = cache[(area, all_)] = (v, build(ec))
        return jsonify({**hit[1], "version": v})

    @bp.get("/scenes")
    def scenes_get():
        all_ = request.args.get("all") in ("1", "true")
        return reply("scenes", scenes.FUNCS, lambda ec: scenes.scenes(ec, include_empty=all_), all_)

    for area, (funcs, build) in AREAS.items():
        bp.add_url_rule(f"/{area}", f"desk_{area}", lambda a=area, f=funcs, b=build: reply(a, f, b))
