"""The desk's USB recorder, read-only over the TF Editor protocol (served by console/desk.py).

    Status/Recorder/Transport/{State,RecState,PlayContinue}          transport (enums PENDING)
    Status/Recorder/PlayList/{DeviceKind,DirPath,TotalSongs,State,Sort/*}
    Status/Recorder/PlayList/Current/{SongNo,FileName,PlayTime,PlayPosition}   seconds
    Status/Recorder/PlayList/Song:i/{FileName,ArtistName,PlayTime,CheckMark,Attribute}  150 slots
    Setup/Recorder/Record/{Level,Format,Source/Select}, Setup/Recorder/Playback/{Level,Mode,AutoScroll}

Values are raw (levels dB x 100); the State / RecState / Mode / Source numbers are shown as
numbers because what each means is unverified.

Transport control would be the event ``Transport "OPERATE:<op> FILENAME:<f> NUMBER:<n>"``, but
the ``<op>`` strings are unknown (docs/desk-coverage-howto-complete.md 3.8). So nothing here
sends anything: TRANSPORT_OPS is where verified op strings go once a TF Editor capture shows
them; sending them would also need a deliberate guard.py change (with tests in
tests/editor/test_guard.py). Until then the page shows the controls as unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .editor.client import TFEditorClient

# The recorder's controls, as the desk's user-defined-key list names them.
CONTROLS = ("Play/Pause", "Stop", "Previous", "Next", "Rec", "Auto Rec", "Rec & Start")
# control -> verified OPERATE string of the Transport event. EMPTY until captured from TF Editor.
TRANSPORT_OPS: dict[str, str] = {}
TRANSPORT_UNVERIFIED = "transport commands unverified (the Transport event's OPERATE strings are not known yet)"
NOT_SENT = "verified, but guard.py doesn't allow the Transport event yet"
MAX_SONGS = 150  # Song[150]


def _get(ec: TFEditorClient, path: str, func: str = "Status") -> int | str | None:
    return ec.get(f"Recorder/{path}", func)


def _db(v: int | str | None) -> float | None:
    return v / 100 if isinstance(v, int) else None


def recorder(ec: TFEditorClient) -> dict[str, Any]:
    """Transport, now playing, playlist and the recorder's setup, as the desk reports them."""
    funcs = ec.status()["functions"]
    total = _get(ec, "PlayList/TotalSongs")
    count = min(total, MAX_SONGS) if isinstance(total, int) else 0
    songs = [
        {
            "index": i,
            "file": _get(ec, f"PlayList/Song:{i}/FileName"),
            "artist": _get(ec, f"PlayList/Song:{i}/ArtistName"),
            "play_time": _get(ec, f"PlayList/Song:{i}/PlayTime"),
            "checked": bool(_get(ec, f"PlayList/Song:{i}/CheckMark")),
            "attribute": _get(ec, f"PlayList/Song:{i}/Attribute"),
        }
        for i in range(count)
    ]
    return {
        "synced": bool(funcs.get("Status", {}).get("synced")),
        "setup_synced": bool(funcs.get("Setup", {}).get("synced")),
        "transport": {
            "state": _get(ec, "Transport/State"),
            "rec_state": _get(ec, "Transport/RecState"),
            "play_continue": _get(ec, "Transport/PlayContinue"),
        },
        "current": {
            "song_no": _get(ec, "PlayList/Current/SongNo"),
            "file": _get(ec, "PlayList/Current/FileName"),
            "play_time": _get(ec, "PlayList/Current/PlayTime"),
            "position": _get(ec, "PlayList/Current/PlayPosition"),
        },
        "playlist": {
            "device_kind": _get(ec, "PlayList/DeviceKind"),
            "dir": _get(ec, "PlayList/DirPath"),
            "total": total,
            "state": _get(ec, "PlayList/State"),
            "sort": {"mode": _get(ec, "PlayList/Sort/Mode"), "direction": _get(ec, "PlayList/Sort/Direction")},
            "songs": songs,
        },
        "setup": {
            "record_level_db": _db(_get(ec, "Record/Level", "Setup")),
            "format": _get(ec, "Record/Format", "Setup"),
            "source": _get(ec, "Record/Source/Select", "Setup"),
            "playback_level_db": _db(_get(ec, "Playback/Level", "Setup")),
            "playback_mode": _get(ec, "Playback/Mode", "Setup"),
            "auto_scroll": _get(ec, "Playback/AutoScroll", "Setup"),
        },
        "controls": [  # never available yet: even a verified op needs the guard change to be sent
            {"name": c, "available": False, "reason": NOT_SENT if c in TRANSPORT_OPS else TRANSPORT_UNVERIFIED}
            for c in CONTROLS
        ],
        "read_only": True,
        "reason": TRANSPORT_UNVERIFIED,
    }
