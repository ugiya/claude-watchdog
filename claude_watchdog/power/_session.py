"""Owned power-session lifecycle."""

from __future__ import annotations

from .. import models as models_module
from . import _contracts as contracts_module
from . import _selection as selection_module
from . import _types as types_module


class PowerSession:
    def __init__(
        self,
        policy: types_module.PowerPolicy,
        bundle: contracts_module.AdapterBundle,
    ) -> None:
        self._policy = policy
        self._bundle = bundle
        self._closed = False

    def __enter__(self) -> PowerSession:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def poll(self, *, check_user_idle: bool) -> types_module.PowerStatus:
        if self._closed or not self._bundle.keep_awake.healthy():
            raise models_module.PowerCommandError("keep-awake hold is no longer active")
        if not check_user_idle or self._policy.user_idle_seconds == 0:
            idle = types_module.IdleObservation(
                kind=types_module.IdleKind.DISABLED, seconds=0.0, source="disabled"
            )
        else:
            idle = self._bundle.idle.observe(self._policy.user_idle_seconds)
            if idle.kind is types_module.IdleKind.UNKNOWN:
                raise models_module.PresenceCheckError(
                    f"user-idle state is unavailable ({idle.source}); re-run with "
                    "--user-idle-minutes 0 to sleep on session quiet alone"
                )
        return types_module.PowerStatus(
            idle=idle,
            keep_awake=self._bundle.keep_awake.name,
            suspend=self._bundle.suspend.name,
            authorization=self._bundle.authorization,
        )

    def request_suspend(self) -> None:
        self._bundle.suspend.request(dry_run=self._policy.dry_run)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bundle.keep_awake.release()


def open_session(
    policy: types_module.PowerPolicy | None = None,
    *,
    platform: str | None = None,
) -> PowerSession:
    selected = policy or types_module.PowerPolicy()
    bundle = selection_module.select_adapters(platform_name=platform)
    session = PowerSession(selected, bundle)
    try:
        bundle.keep_awake.acquire()
    except OSError as exc:
        bundle.keep_awake.release()
        raise models_module.PowerCommandError(
            f"unable to start keep-awake hold: {exc}"
        ) from exc
    if not bundle.keep_awake.healthy():
        bundle.keep_awake.release()
        raise models_module.PowerCommandError("keep-awake hold exited immediately")
    return session
