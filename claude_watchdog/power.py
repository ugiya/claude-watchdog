"""Presence and owned power-management commands for macOS and Linux.

Each platform answers the same three questions with its own native tools:

===============  ==========================  ==========================
operation        macOS                       Linux
===============  ==========================  ==========================
user presence    ``ioreg`` HIDIdleTime       desktop idle probes, in
                                             preference order
hold awake       ``caffeinate -is -w PID``   ``systemd-inhibit`` (or
                                             ``gnome-session-inhibit``)
sleep now        ``pmset sleepnow``          ``systemctl suspend`` (or
                                             ``loginctl suspend``)
===============  ==========================  ==========================

The owned wake assertion is always a child process that dies with this one, so
an unclean exit can never strand the machine awake. On macOS ``caffeinate -w``
polls the watched PID; on Linux the child inherits a pipe whose write end this
process owns, so the kernel closing that end at exit releases the inhibitor
immediately.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

from . import models as models_module


DARWIN = "darwin"
LINUX = "linux"

#: Presence probes are interactive-latency helpers, not long-running commands.
PRESENCE_PROBE_TIMEOUT_SECONDS = 5.0

#: ``gdbus call`` prints a single-element tuple such as ``(uint64 1234,)``.
_GDBUS_SCALAR = re.compile(r"\A\(\s*(?:[A-Za-z][A-Za-z0-9]*\s+)?(\d+)\s*,?\s*\)\s*\Z")

_WAKE_ASSERTION_REASON = "claude-watchdog is watching agent sessions"


def _backend() -> str | None:
    """Return the power backend for this platform, or None when unsupported."""
    if sys.platform == DARWIN:
        return DARWIN
    if sys.platform.startswith(LINUX):
        return LINUX
    return None


# --------------------------------------------------------------------- macOS


def _darwin_user_idle_seconds() -> float:
    """Return HID idle seconds, or raise when macOS presence is unavailable."""
    try:
        output = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise models_module.PresenceCheckError(f"unable to query HID idle time: {exc}") from exc

    for line in output.splitlines():
        if "HIDIdleTime" not in line:
            continue
        try:
            value = int(line.rsplit("=", 1)[1].strip()) / 1_000_000_000
        except (ValueError, IndexError) as exc:
            raise models_module.PresenceCheckError("ioreg returned an invalid HIDIdleTime value") from exc
        if value < 0:
            raise models_module.PresenceCheckError("ioreg returned a negative HIDIdleTime value")
        return value
    raise models_module.PresenceCheckError("ioreg output did not contain HIDIdleTime")


def _darwin_block_sleep() -> subprocess.Popen:
    return subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])


# --------------------------------------------------------------------- Linux


def _probe(argv: list[str]) -> str | None:
    """Run a bounded read-only probe, returning stdout only on clean success."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PRESENCE_PROBE_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
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


def _idle_from_mutter() -> float | None:
    """GNOME (Wayland and X11) exposes idle milliseconds on the session bus."""
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
    return None if value is None else value / 1000.0


def _idle_from_screensaver() -> float | None:
    """KDE, Xfce and Cinnamon answer GetSessionIdleTime in whole seconds."""
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
    return None if value is None else float(value)


def _idle_from_xprintidle() -> float | None:
    """Any X11 session with xprintidle installed reports idle milliseconds."""
    output = _probe(["xprintidle"])
    if output is None:
        return None
    try:
        return int(output.strip()) / 1000.0
    except ValueError:
        return None


def _logind_session_id() -> str | None:
    session = os.environ.get("XDG_SESSION_ID", "").strip()
    if session:
        return session
    output = _probe(["loginctl", "show-user", str(os.getuid()), "--property=Display", "--value"])
    if output is None:
        return None
    session = output.strip()
    return session or None


def _idle_from_logind() -> float | None:
    """Use logind's idle hint, but only when a session actually reports idle.

    ``IdleHint=no`` is ambiguous: it means either the user is present or the
    compositor never reports idle at all (COSMIC, sway and other wlroots
    desktops do not). Treating that as "user present" would silently hold the
    machine awake forever, so it is reported as no answer instead.
    """
    session = _logind_session_id()
    if not session:
        return None
    output = _probe(
        ["loginctl", "show-session", session, "-p", "IdleHint", "-p", "IdleSinceHintMonotonic"]
    )
    if output is None:
        return None
    fields = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )
    if fields.get("IdleHint", "").strip() != "yes":
        return None
    try:
        since_microseconds = int(fields.get("IdleSinceHintMonotonic", "").strip())
    except ValueError:
        return None
    if since_microseconds <= 0:
        return None
    now_microseconds = time.clock_gettime(time.CLOCK_MONOTONIC) * 1_000_000
    return max(0.0, (now_microseconds - since_microseconds) / 1_000_000)


#: Ordered idle-time probes: the first that answers wins.
LINUX_PRESENCE_SOURCES: tuple[tuple[str, object], ...] = (
    ("GNOME Mutter IdleMonitor", _idle_from_mutter),
    ("org.freedesktop.ScreenSaver", _idle_from_screensaver),
    ("xprintidle", _idle_from_xprintidle),
    ("systemd-logind idle hint", _idle_from_logind),
)


def _linux_user_idle_seconds() -> float:
    for name, probe in LINUX_PRESENCE_SOURCES:
        value = probe()
        if value is None:
            continue
        if value < 0:
            raise models_module.PresenceCheckError(f"{name} reported a negative idle time")
        return float(value)
    raise models_module.PresenceCheckError(
        "no desktop idle-time source answered (tried "
        + ", ".join(name for name, _ in LINUX_PRESENCE_SOURCES)
        + "); re-run with --user-idle-minutes 0 to drop the user-presence gate"
    )


def _linux_inhibit_command(executable: str, name: str) -> list[str]:
    reason = f"{_WAKE_ASSERTION_REASON} (pid {os.getpid()})"
    if name == "systemd-inhibit":
        return [
            executable,
            "--what=idle:sleep",
            "--who=claude-watchdog",
            f"--why={reason}",
            "--mode=block",
            "cat",
        ]
    return [executable, "--inhibit", "suspend:idle", "--reason", reason, "--command", "cat"]


#: Ordered sleep inhibitors: the first one installed is used.
LINUX_INHIBITORS: tuple[str, ...] = ("systemd-inhibit", "gnome-session-inhibit")


def _linux_block_sleep() -> subprocess.Popen:
    """Hold the machine awake with a child that dies when this process does.

    ``cat`` blocks on a pipe whose write end belongs to this process, so the
    inhibitor is released the moment this process exits for any reason --
    including SIGKILL, which no cleanup handler could catch.
    """
    for name in LINUX_INHIBITORS:
        executable = shutil.which(name)
        if executable is None:
            continue
        return subprocess.Popen(
            _linux_inhibit_command(executable, name),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
    raise OSError(
        "no sleep inhibitor is installed; expected one of "
        + ", ".join(LINUX_INHIBITORS)
    )


# ------------------------------------------------------- platform dispatch


def user_idle_seconds() -> float:
    """Return seconds since the last user input, or raise when unavailable."""
    backend = _backend()
    if backend == DARWIN:
        return _darwin_user_idle_seconds()
    if backend == LINUX:
        return _linux_user_idle_seconds()
    raise models_module.PresenceCheckError(
        f"user presence detection is not supported on {sys.platform}"
    )


def presence_source() -> str | None:
    """Name the idle-time source this machine will use, or None when there is none.

    Callers use this to warn at startup instead of failing once sessions go
    quiet, hours into a watch.
    """
    backend = _backend()
    if backend == DARWIN:
        try:
            _darwin_user_idle_seconds()
        except models_module.PresenceCheckError:
            return None
        return "macOS HID idle time"
    if backend == LINUX:
        for name, probe in LINUX_PRESENCE_SOURCES:
            if probe() is not None:
                return name
    return None


def block_sleep() -> subprocess.Popen:
    """Hold the machine awake (idle + system sleep) until this process exits."""
    backend = _backend()
    if backend == DARWIN:
        return _darwin_block_sleep()
    if backend == LINUX:
        return _linux_block_sleep()
    raise OSError(f"holding the machine awake is not supported on {sys.platform}")


def _stop_wake_assertion(process: subprocess.Popen) -> None:
    """Release an owned wake assertion, closing its lifetime pipe first."""
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass  # The child already exited and closed the read end.
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except ProcessLookupError:
        pass  # The owned child exited between poll() and terminate().


#: Retained for callers written against the macOS-only API.
_stop_caffeinate = _stop_wake_assertion


def sleep_commands() -> tuple[list[str], ...]:
    """Return the sleep commands to try, in order, for this platform."""
    backend = _backend()
    if backend == DARWIN:
        return (["pmset", "sleepnow"],)
    if backend == LINUX:
        return (["systemctl", "suspend"], ["loginctl", "suspend"])
    return ()


def force_sleep(dry_run: bool) -> None:
    """Put the machine to sleep now, unless ``dry_run`` is enabled."""
    commands = sleep_commands()
    if not commands:
        raise models_module.PowerCommandError(f"sleeping is not supported on {sys.platform}")
    if dry_run:
        models_module.log.info("[dry-run] would run: %s", " ".join(commands[0]))
        return

    failures: list[str] = []
    for argv in commands:
        printable = " ".join(argv)
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True)
        except OSError as exc:
            failures.append(f"unable to run {printable}: {exc}")
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            suffix = f": {detail[:300]}" if detail else ""
            failures.append(f"{printable} failed with exit status {exc.returncode}{suffix}")
        else:
            return
    raise models_module.PowerCommandError("; ".join(failures))
