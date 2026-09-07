"""macOS presence and owned power-management commands."""

from __future__ import annotations

import os
import subprocess

from . import models as models_module

def user_idle_seconds() -> float:
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


def block_sleep() -> subprocess.Popen:
    """Hold the Mac awake (idle + system sleep) until this process exits."""
    return subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])


def _stop_caffeinate(process: subprocess.Popen) -> None:
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


def force_sleep(dry_run: bool) -> None:
    """Force the Mac to sleep now, unless ``dry_run`` is enabled."""
    if dry_run:
        models_module.log.info("[dry-run] would run: pmset sleepnow")
        return
    try:
        subprocess.run(
            ["pmset", "sleepnow"],
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise models_module.PowerCommandError(f"unable to run pmset sleepnow: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail[:300]}" if detail else ""
        raise models_module.PowerCommandError(
            f"pmset sleepnow failed with exit status {exc.returncode}{suffix}"
        ) from exc
