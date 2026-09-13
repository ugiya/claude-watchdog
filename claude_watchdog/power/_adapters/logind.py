"""systemd-logind keep-awake, session idle, and suspend adapters."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from ... import models as models_module
from .. import _types as types_module


def logind_tools_available() -> bool:
    return shutil.which("systemd-inhibit") is not None and shutil.which("systemctl") is not None


def can_suspend() -> str:
    busctl = shutil.which("busctl")
    if busctl is None:
        return "unknown"
    try:
        output = subprocess.check_output(
            [
                busctl,
                "--system",
                "--allow-interactive-authorization=no",
                "--timeout=5s",
                "call",
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "CanSuspend",
            ],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    text = output.strip().strip('"')
    if text.startswith("s "):
        text = text[2:].strip().strip('"')
    return text or "unknown"


class LogindKeepAwake:
    name = "logind"

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None

    def acquire(self) -> None:
        inhibit = shutil.which("systemd-inhibit")
        if inhibit is None:
            raise models_module.PowerCommandError("systemd-inhibit is not available")
        # `cat` blocks on a pipe whose write end this process owns, so the
        # kernel closing that end releases the inhibitor the moment the
        # watchdog exits -- including on SIGKILL, where no cleanup can run.
        # `sleep infinity` would outlive an unclean exit and strand the
        # machine awake. This mirrors what `caffeinate -w <pid>` gives macOS.
        self._process = subprocess.Popen(
            [
                inhibit,
                "--what=idle:sleep",
                "--mode=block",
                "--who=claude-watchdog",
                f"--why=Watching selected agent activity (pid {os.getpid()})",
                "cat",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )

    def release(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
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
            pass

    def healthy(self) -> bool:
        return self._process is not None and self._process.poll() is None


class LogindIdle:
    name = "logind-session"

    def observe(self, threshold_seconds: float) -> types_module.IdleObservation:
        session = os.environ.get("XDG_SESSION_ID", "").strip()
        loginctl = shutil.which("loginctl")
        if not session or loginctl is None:
            return types_module.IdleObservation(
                kind=types_module.IdleKind.UNKNOWN, source=self.name
            )
        try:
            output = subprocess.check_output(
                [
                    loginctl,
                    "show-session",
                    session,
                    "-p",
                    "IdleHint",
                    "-p",
                    "IdleSinceHintMonotonic",
                ],
                text=True,
                stderr=subprocess.STDOUT,
            )
        except (OSError, subprocess.CalledProcessError):
            return types_module.IdleObservation(
                kind=types_module.IdleKind.UNKNOWN, source=self.name
            )
        hint = None
        since_us = None
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key == "IdleHint":
                hint = value.strip().lower()
            elif key == "IdleSinceHintMonotonic":
                try:
                    since_us = int(value.strip())
                except ValueError:
                    since_us = None
        if hint != "yes" or since_us is None:
            # `IdleHint=no` is ambiguous: either the user is present, or this
            # compositor never sets the hint at all. wlroots desktops (COSMIC,
            # sway, Hyprland) report idle only over the Wayland
            # ext-idle-notify-v1 protocol and leave the hint at "no" forever,
            # so reading it as presence holds the machine awake indefinitely
            # while the log claims the user is active. Report no answer and let
            # a higher-priority observer, or an explicit error, decide.
            return types_module.IdleObservation(
                kind=types_module.IdleKind.UNKNOWN, source=self.name
            )
        age = max(0.0, time.clock_gettime(time.CLOCK_MONOTONIC) - (since_us / 1_000_000))
        kind = (
            types_module.IdleKind.READY
            if age >= threshold_seconds
            else types_module.IdleKind.WAITING
        )
        return types_module.IdleObservation(kind=kind, seconds=age, source=self.name)


class LogindSuspend:
    name = "logind"

    def request(self, *, dry_run: bool) -> None:
        if dry_run:
            models_module.log.info(
                "[dry-run] would run: systemctl --no-ask-password --check-inhibitors=yes suspend"
            )
            return
        systemctl = shutil.which("systemctl")
        if systemctl is None:
            raise models_module.PowerCommandError("systemctl is not available")
        try:
            subprocess.run(
                [systemctl, "--no-ask-password", "--check-inhibitors=yes", "suspend"],
                check=True,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise models_module.PowerCommandError(
                f"unable to run systemctl suspend: {exc}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            suffix = f": {detail[:300]}" if detail else ""
            raise models_module.PowerCommandError(
                f"systemctl suspend failed with exit status {exc.returncode}{suffix}"
            ) from exc
