"""Select compatible power adapters for the current host."""

from __future__ import annotations

import sys

from .. import models as models_module
from . import _contracts as contracts_module
from ._adapters import desktop as desktop_module
from ._adapters import logind as logind_module
from ._adapters import macos as macos_module
from . import _types as types_module


class IdleChain:
    """Ask each observer in order and take the first that can answer.

    Reporting UNKNOWN when every observer declines is deliberate. The
    alternative -- treating "no answer" as "the user is here" -- holds the
    machine awake forever on desktops that never publish idle time, and does it
    silently, which is the worst of both outcomes.
    """

    def __init__(self, observers: tuple[contracts_module.IdleObserver, ...]) -> None:
        self._observers = observers
        self.name = "+".join(observer.name for observer in observers)

    def observe(self, threshold_seconds: float) -> types_module.IdleObservation:
        for observer in self._observers:
            observation = observer.observe(threshold_seconds)
            if observation.kind is not types_module.IdleKind.UNKNOWN:
                return observation
        return types_module.IdleObservation(
            kind=types_module.IdleKind.UNKNOWN,
            source="tried " + ", ".join(observer.name for observer in self._observers),
        )


def linux_idle_observer() -> contracts_module.IdleObserver:
    """Order Linux idle sources from most to least authoritative."""
    return IdleChain(
        (
            desktop_module.MutterIdle(),
            desktop_module.ScreenSaverIdle(),
            desktop_module.XPrintIdle(),
            logind_module.LogindIdle(),
        )
    )


def select_adapters(*, platform_name: str | None = None) -> contracts_module.AdapterBundle:
    name = platform_name or sys.platform
    if name == "darwin":
        return contracts_module.AdapterBundle(
            keep_awake=macos_module.MacKeepAwake(),
            idle=macos_module.MacIdle(),
            suspend=macos_module.MacSuspend(),
            authorization="local",
        )
    if name.startswith("linux"):
        if not logind_module.logind_tools_available():
            raise models_module.PowerCommandError(
                "Linux sleep support requires systemd-inhibit and systemctl"
            )
        capability = logind_module.can_suspend()
        if capability not in {"yes", "unknown"}:
            raise models_module.PowerCommandError(
                f"logind reports suspend is not currently permitted ({capability})"
            )
        return contracts_module.AdapterBundle(
            keep_awake=logind_module.LogindKeepAwake(),
            idle=linux_idle_observer(),
            suspend=logind_module.LogindSuspend(),
            authorization=capability,
        )
    raise models_module.PowerCommandError(f"unsupported platform: {name}")
