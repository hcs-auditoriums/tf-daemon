"""Small file helpers shared by the modules that persist state."""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, text: str, *, keep_mode: bool = False, parents: bool = False) -> None:
    """Write ``text`` to ``path`` so readers see the old file or the new one, never half.

    ``keep_mode``: the new file gets the old one's permissions (a config the admin
    locked down stays locked). ``parents``: create missing parent directories."""
    if parents and path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if keep_mode and path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
