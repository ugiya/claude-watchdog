"""Public power-session types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IdleKind(str, Enum):
    WAITING = "waiting"
    READY = "ready"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


@dataclass(frozen=True)
class PowerPolicy:
    user_idle_seconds: float = 300.0
    dry_run: bool = False


@dataclass(frozen=True)
class IdleObservation:
    kind: IdleKind
    seconds: float | None = None
    source: str = "none"


@dataclass(frozen=True)
class PowerStatus:
    idle: IdleObservation
    keep_awake: str
    suspend: str
    authorization: str = "unknown"
