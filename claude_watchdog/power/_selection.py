"""Select compatible power adapters for the current host."""

from __future__ import annotations

import sys

from .. import models as models_module
from . import _contracts as contracts_module
from ._adapters import logind as logind_module
from ._adapters import macos as macos_module


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
            idle=logind_module.LogindIdle(),
            suspend=logind_module.LogindSuspend(),
            authorization=capability,
        )
    raise models_module.PowerCommandError(f"unsupported platform: {name}")
