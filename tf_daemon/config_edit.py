"""Editing config.toml one setting at a time, for the /config page (routes in system.py).

``settings()`` describes every setting of every ``[table]`` in config.py: its type, the value
the file gives it, its default, whether the file sets it, what it does (the comment beside it
in config.py) and, where it has a fixed set, its choices. ``set_value`` / ``unset_value``
change one line of the file's text and leave everything else (comments, order, the
``[[channels]]`` tables) as it was; the caller validates the result before writing it.
"""

from __future__ import annotations

import inspect
import json
import re
import tomllib
from dataclasses import fields
from typing import Any

from .config import AUDIO_SOURCES, DEVICE_SOURCES, SECTIONS, Config
from .rf.settings import RBW_CHOICES
from .vocal.targets import EQ_STYLES, MIC_TYPES

# What the page calls each table, in the order it shows them.
TITLES = {
    "tf5": "TF5 console",
    "vocal": "Vocal EQ assist",
    "ring": "Ring detection",
    "audio": "Audio input",
    "rf": "RF (tinySA)",
    "qlxd": "Wireless (QLX-D)",
    "dsp": "Analysis",
    "synth": "Demo audio",
    "server": "Server",
    "update": "Updates",
}

# The settings changed most, shown together at the top of the page.
COMMON = ("tf5.eq_write", "tf5.fader_write", "vocal.auto_allowed", "rf.auto_connect")

CHOICES: dict[str, tuple[Any, ...]] = {
    "audio.source": AUDIO_SOURCES,
    "rf.source": DEVICE_SOURCES,
    "qlxd.source": DEVICE_SOURCES,
    "tf5.source": DEVICE_SOURCES,
    "rf.rbw": tuple(RBW_CHOICES),
    "ring.sensitivity": ("low", "normal", "high"),
    "ring.mains_hz": (60.0, 50.0, 0.0),
    "vocal.mic_type": tuple(MIC_TYPES),
    "vocal.eq_style": tuple(EQ_STYLES),
}

_FIELD_COMMENT = re.compile(r"^\s{4}(\w+)\s*:[^=]*=.*?#\s?(.*)$")


def _comments(cls: type) -> dict[str, str]:
    """The comment beside each field in config.py: what the setting does."""
    out = {}
    for line in inspect.getsource(cls).splitlines():
        m = _FIELD_COMMENT.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _kind(default: Any) -> str:
    for t, name in ((bool, "bool"), (int, "int"), (float, "float"), (str, "str"), (tuple, "list")):
        if isinstance(default, t):
            return name
    raise AssertionError(f"no kind for {default!r}")


def _plain(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


def settings(cfg: Config, text: str) -> list[dict[str, Any]]:
    """Every table's settings as the file sets them (see the module docstring)."""
    in_file = tomllib.loads(text)
    out = []
    for name in TITLES:
        cls = SECTIONS[name]
        section = getattr(cfg, name)
        help_ = _comments(cls)
        doc = (inspect.getdoc(cls) or "").split("\n\n")[0]
        if doc.startswith(f"{cls.__name__}("):  # no docstring: the dataclass's generated signature
            doc = ""
        found = in_file.get(name)
        table: dict[str, Any] = found if isinstance(found, dict) else {}
        out.append(
            {
                "name": name,
                "title": TITLES[name],
                "doc": doc.replace("\n", " "),
                "settings": [
                    {
                        "key": f"{name}.{f.name}",
                        "kind": _kind(f.default),
                        "value": _plain(getattr(section, f.name)),
                        "default": _plain(f.default),
                        "set": f.name in table,
                        "help": help_.get(f.name, ""),
                        "choices": list(CHOICES.get(f"{name}.{f.name}", ())),
                    }
                    for f in fields(cls)
                ],
            }
        )
    return out


def parse_value(key: str, value: Any) -> Any:
    """``value`` from the page as the setting's type; ValueError naming the key if it can't be."""
    table, name = _split(key)
    default = next(f.default for f in fields(SECTIONS[table]) if f.name == name)
    kind = _kind(default)
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if kind in ("int", "float"):
        if isinstance(value, str):
            value = value.strip()
            try:
                value = float(value) if value else None
            except ValueError:
                raise ValueError(f"{key} must be a number, got {value!r}") from None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{key} must be a number")
        if kind == "int":
            if value != int(value):
                raise ValueError(f"{key} must be a whole number, got {value}")
            return int(value)
        return float(value)
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        return value
    # A list: typed on the page as items separated by commas; numbers stay numbers.
    items = value if isinstance(value, list) else [s.strip() for s in str(value).split(",") if s.strip()]
    return [int(i) if isinstance(i, str) and re.fullmatch(r"-?\d+", i) else i for i in items]


def _split(key: str) -> tuple[str, str]:
    table, _, name = key.partition(".")
    if table not in SECTIONS or name not in {f.name for f in fields(SECTIONS[table])}:
        raise ValueError(f"no setting {key!r}")
    return table, name


def _literal(value: Any) -> str:
    """``value`` written as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a TOML basic string


# One setting's line: its key, its value (a string, a one-line array or a bare word) and
# whatever follows (spaces, a comment).
def _line(name: str) -> re.Pattern[str]:
    value = r"\"(?:[^\"\\]|\\.)*\"|'[^']*'|\[[^\]\n]*\]|[^\s#]+"
    return re.compile(rf"^(\s*{re.escape(name)}\s*=\s*)({value})(.*)$", re.S)


def _table_lines(lines: list[str], table: str) -> tuple[int | None, int]:
    """The header line of ``[table]`` (None if missing) and where the table ends."""
    start = None
    for i, line in enumerate(lines):
        header = re.match(r"\s*\[\[?([^\[\]]+)\]\]?\s*(#.*)?$", line)
        if header and start is not None:
            return start, i
        if header and header.group(1).strip() == table and not line.lstrip().startswith("[["):
            start = i
    return start, len(lines)


def set_value(text: str, key: str, value: Any) -> str:
    """``text`` with ``key = value`` in its table: the line changed in place (its comment kept),
    else added under the table's header, else the table added at the end."""
    table, name = _split(key)
    lit = _literal(value)
    lines = text.splitlines(keepends=True)
    start, end = _table_lines(lines, table)
    if start is None:
        sep = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
        return f"{text}{sep}[{table}]\n{name} = {lit}\n"
    pattern = _line(name)
    for i in range(start + 1, end):
        m = pattern.match(lines[i])
        if m:
            if m.group(2).startswith("[") and not m.group(2).endswith("]"):
                raise ValueError(f"{key} spans several lines in config.toml: edit it there")
            lines[i] = f"{m.group(1)}{lit}{m.group(3)}"
            return "".join(lines)
        if re.match(rf"^\s*{re.escape(name)}\s*=", lines[i]):
            raise ValueError(f"{key} spans several lines in config.toml: edit it there")
    lines.insert(start + 1, f"{name} = {lit}\n")
    return "".join(lines)


def unset_value(text: str, key: str) -> str:
    """``text`` without ``key``'s line: the setting goes back to its default."""
    table, name = _split(key)
    lines = text.splitlines(keepends=True)
    start, end = _table_lines(lines, table)
    if start is not None:
        for i in range(start + 1, end):
            m = _line(name).match(lines[i])
            if m and not (m.group(2).startswith("[") and not m.group(2).endswith("]")):
                del lines[i]
                break
            if re.match(rf"^\s*{re.escape(name)}\s*=", lines[i]):
                raise ValueError(f"{key} spans several lines in config.toml: edit it there")
    return "".join(lines)
