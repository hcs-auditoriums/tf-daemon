"""The desk's scene memory, read over the TF Editor protocol (served by console/desk.py).

    SceneStatus/Bank:b/Info:i/{DataType,Protect,Title,Comment,TimeStamp/*,Fading/{FadeTime,On}}
    SceneList/Bank:b/Info:i/FileName              the .tfs file per slot
    Setup/Scene/{LastScene,EditFlag}              the scene last recalled ("A05"), edited since
    ConsoleSetup/Scene/SelectedScene              the scene selected on the desk's scene screen

Bank 0 is A, 1 is B, 100 slots each, numbered as the desk shows them: "A00" .. "B99".
A slot counts as stored when its DataType is 1, or it has a title, comment or time stamp
(DataType's meaning is [INFERRED]: 0 / 1 in the schema, 0 by default; unverified on a desk).

Recall goes through TFEditorClient.recall_scene (``RecallScene "NUMBER:A05"``, guard.py, only
with tf5.scene_recall and a person's confirmed click). Storing a scene and undoing a recall have
no known wire command (docs/desk-coverage-howto-complete.md 3.6), so they are reported as
unavailable with the reason, never attempted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .editor.client import TFEditorClient

BANKS = ("A", "B")
SLOTS = 100
FUNCS = ("SceneStatus", "SceneList", "Setup", "ConsoleSetup")  # what the scene view reads
STORE_UNAVAILABLE = (
    "storing a scene has no known TF Editor command (TF Editor stores through its scene file "
    "transfer; RCP ssupdate is a candidate): unverified, so not offered"
)
UNDO_UNAVAILABLE = "the desk's recall undo has no known remote command: unverified, so not offered"


def number(bank: int, slot: int) -> str:
    """The desk's scene number: ``number(0, 5)`` -> ``"A05"``."""
    return f"{BANKS[bank]}{slot:02d}"


def _str(ec: TFEditorClient, path: str, func: str) -> str:
    v = ec.get(path, func)
    return v if isinstance(v, str) else ""


def _int(ec: TFEditorClient, path: str, func: str) -> int | None:
    v = ec.get(path, func)
    return v if isinstance(v, int) else None


def _stamp(ec: TFEditorClient, base: str) -> str | None:
    """``TimeStamp/*`` as "2026-11-08 19:30:05"; None when unset (year 0)."""
    parts = [
        _int(ec, f"{base}/TimeStamp/{k}", "SceneStatus") for k in ("Year", "Month", "Day", "Hour", "Minute", "Second")
    ]
    if any(p is None for p in parts) or not parts[0]:
        return None
    y, mo, d, h, mi, s = parts
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


def slot(ec: TFEditorClient, bank: int, i: int) -> dict[str, Any]:
    """One scene slot as the page shows it."""
    base = f"Bank:{bank}/Info:{i}"
    data_type = _int(ec, f"{base}/DataType", "SceneStatus")
    fade = _int(ec, f"{base}/Fading/FadeTime", "SceneStatus")
    title, comment, stamp = (
        _str(ec, f"{base}/Title", "SceneStatus"),
        _str(ec, f"{base}/Comment", "SceneStatus"),
        _stamp(ec, base),
    )
    return {
        "number": number(bank, i),
        "bank": BANKS[bank],
        "slot": i,
        "stored": bool(data_type) or bool(title or comment or stamp),
        "data_type": data_type,
        "title": title,
        "comment": comment,
        "protect": bool(_int(ec, f"{base}/Protect", "SceneStatus")),
        "time_stamp": stamp,
        "fade": {
            "on": bool(_int(ec, f"{base}/Fading/On", "SceneStatus")),
            "time_s": None if fade is None else fade / 10,
        },
        "file": _str(ec, f"{base}/FileName", "SceneList") or None,
    }


def current(ec: TFEditorClient) -> dict[str, Any]:
    """The scene last recalled, whether it was edited since, and the desk's selected scene."""
    last = _str(ec, "Scene/LastScene", "Setup") or None
    edited = _int(ec, "Scene/EditFlag", "Setup")
    return {
        "number": last,
        "edited": None if edited is None else bool(edited),
        "selected": _str(ec, "Scene/SelectedScene", "ConsoleSetup") or None,
        "last_recalled": ec.last_recalled,  # the desk's last onRecalledScene (any client's recall)
    }


def scenes(ec: TFEditorClient, include_empty: bool = False) -> dict[str, Any]:
    """Both banks (stored slots only unless ``include_empty``), the current scene, and what
    may be done: recall (tf5.scene_recall), store and undo (never: no known command)."""
    funcs = ec.status()["functions"]
    synced = all(funcs.get(f, {}).get("synced") for f in ("SceneStatus", "Setup"))
    rows = [slot(ec, b, i) for b in range(len(BANKS)) for i in range(SLOTS)] if synced else []
    shown = [r for r in rows if include_empty or r["stored"]]
    return {
        "synced": synced,
        "subscribed": {f: f in funcs for f in FUNCS},
        "current": current(ec),
        "scenes": shown,
        "stored": sum(r["stored"] for r in rows),
        "slots": len(BANKS) * SLOTS,
        "recall": {
            "available": ec.scene_recall,
            "reason": None if ec.scene_recall else "recalling scenes is off (tf5.scene_recall = false)",
        },
        "store": {"available": False, "reason": STORE_UNAVAILABLE},
        "undo": {"available": False, "reason": UNDO_UNAVAILABLE},
    }
