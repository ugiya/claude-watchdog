"""Desktop human-idle adapters for Linux sessions.

Linux has no single idle-time API. Each desktop answers a different question in
a different unit, and wlroots compositors answer none of them over D-Bus, so
these adapters are tried in priority order and every one reports UNKNOWN rather
than guessing when it cannot speak for the session.

=============================  ===========================  ==============
adapter                        covers                       unit returned
=============================  ===========================  ==============
``gnome-mutter``               GNOME, Wayland and X11       milliseconds
``freedesktop-screensaver``    KDE, Xfce, Cinnamon          seconds
``xprintidle``                 any X11 session with it      milliseconds
=============================  ===========================  ==============
"""

from __future__ import annotations

import re
import subprocess

from .. import _types as types_module


#: Idle probes are interactive-latency helpers, not long-running commands.
PROBE_TIMEOUT_SECONDS = 5.0

#: ``gdbus call`` prints a single-element tuple such as ``(uint64 1234,)``.
_GDBUS_SCALAR = re.compile(r"\A\(\s*(?:[A-Za-z][A-Za-z0-9]*\s+)?(\d+)\s*,?\s*\)\s*\Z")


def _probe(argv: list[str]) -> str | None:
    """Run a bounded read-only probe, returning stdout only on clean success."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return None
    return completed.stdout


def _gdbus_unsigned(output: str | None) -> int | None:
    """Return the single unsigned scalar in a ``gdbus call`` reply."""
    if output is None:
        return None
    match = _GDBUS_SCALAR.match(output.strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _observation(
    seconds: float | None, threshold_seconds: float, source: str
) -> types_module.IdleObservation:
    if seconds is None:
        return types_module.IdleObservation(
            kind=types_module.IdleKind.UNKNOWN, source=source
        )
    if seconds < 0:
        return types_module.IdleObservation(
            kind=types_module.IdleKind.UNKNOWN, source=source
        )
    kind = (
        types_module.IdleKind.READY
        if seconds >= threshold_seconds
        else types_module.IdleKind.WAITING
    )
    return types_module.IdleObservation(kind=kind, seconds=seconds, source=source)


class MutterIdle:
    """GNOME's IdleMonitor, on both Wayland and X11."""

    name = "gnome-mutter"

    def observe(self, threshold_seconds: float) -> types_module.IdleObservation:
        value = _gdbus_unsigned(
            _probe(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.gnome.Mutter.IdleMonitor",
                    "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
                    "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime",
                ]
            )
        )
        seconds = None if value is None else value / 1000.0
        return _observation(seconds, threshold_seconds, self.name)


class ScreenSaverIdle:
    """KDE, Xfce and Cinnamon answer GetSessionIdleTime in whole seconds.

    COSMIC owns this bus name but implements only Inhibit/UnInhibit, so the
    missing method surfaces as a failed call and therefore UNKNOWN.
    """

    name = "freedesktop-screensaver"

    def observe(self, threshold_seconds: float) -> types_module.IdleObservation:
        value = _gdbus_unsigned(
            _probe(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.freedesktop.ScreenSaver",
                    "--object-path", "/org/freedesktop/ScreenSaver",
                    "--method", "org.freedesktop.ScreenSaver.GetSessionIdleTime",
                ]
            )
        )
        seconds = None if value is None else float(value)
        return _observation(seconds, threshold_seconds, self.name)


class XPrintIdle:
    """Any X11 session with xprintidle installed."""

    name = "xprintidle"

    def observe(self, threshold_seconds: float) -> types_module.IdleObservation:
        output = _probe(["xprintidle"])
        seconds: float | None = None
        if output is not None:
            try:
                seconds = int(output.strip()) / 1000.0
            except ValueError:
                seconds = None
        return _observation(seconds, threshold_seconds, self.name)
