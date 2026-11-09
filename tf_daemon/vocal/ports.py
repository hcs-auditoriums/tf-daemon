"""What the vocal assist needs from the rest of tf-daemon, as Protocols (wired in app.build_modules)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..config import Channel
    from ..dsp.ring import RingLog

Tap = Callable[[int, Any, float], None]   # (input, samples, stream time), on the analysis thread
FrameTap = Callable[[dict[int, Any], float], None]   # ({input: samples}, stream time), once per hop


class AudioInputs(Protocol):
    """The audio side (audio.engine.AudioEngine)."""

    @property
    def inputs(self) -> list[Channel]: ...

    @property
    def rings(self) -> RingLog: ...

    def add_tap(self, tap: Tap) -> None: ...

    def add_frame_tap(self, tap: FrameTap) -> None: ...

    def acquire(self, index: int) -> None: ...

    def release(self, index: int) -> None: ...


class DeskEQ(Protocol):
    """The console's EQ, read and (on a person's request) written (console.module.TF5Module)."""

    @property
    def eq_available(self) -> bool: ...

    @property
    def eq_writable(self) -> bool: ...

    def eq(self, usb: int) -> dict[str, Any]: ...

    def eq_target(self, usb: int) -> str | None: ...

    def eq_raw(self, usb: int, paths: list[str]) -> dict[str, int | str | None]: ...

    def write_eq(self, usb: int, changes: list[tuple[str, int]]) -> list[dict[str, Any]]: ...
