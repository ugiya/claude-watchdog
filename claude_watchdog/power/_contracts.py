"""Private adapter contracts for keep-awake, idle, and suspend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from . import _types as types_module


class KeepAwake(Protocol):
    name: str

    def acquire(self) -> None:
        """Establish an owned keep-awake hold."""

    def release(self) -> None:
        """Release the owned hold. Safe to call more than once."""

    def healthy(self) -> bool:
        """Return whether the owned hold is still active."""


class IdleObserver(Protocol):
    name: str

    def observe(self, threshold_seconds: float) -> types_module.IdleObservation:
        """Return the current human-idle observation."""


class SuspendRequester(Protocol):
    name: str

    def request(self, *, dry_run: bool) -> None:
        """Submit a suspend request, or log it when dry_run is true."""


@dataclass(frozen=True)
class AdapterBundle:
    keep_awake: KeepAwake
    idle: IdleObserver
    suspend: SuspendRequester
    authorization: str
