"""Platform-neutral power session: keep-awake, human idle, and suspend."""

from __future__ import annotations

import subprocess

from ._adapters.macos import _stop_caffeinate, block_sleep, force_sleep, user_idle_seconds
from ._session import PowerSession, open_session
from ._types import IdleKind, IdleObservation, PowerPolicy, PowerStatus

__all__ = (
    "IdleKind",
    "IdleObservation",
    "PowerPolicy",
    "PowerSession",
    "PowerStatus",
    "open_session",
    "block_sleep",
    "force_sleep",
    "user_idle_seconds",
    "_stop_caffeinate",
)
