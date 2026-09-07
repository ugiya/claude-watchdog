#!/usr/bin/env python3
"""Isolated end-to-end checks for live Codex and Claude profile discovery.

This script never uses the caller's home, Codex tree, log file, or power tools.
It invokes the worktree executable with ``--dry-run`` and places fail-closed
``pmset`` plus PID-recording ``caffeinate`` shims first on the child PATH.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET = Path(os.environ.get("WATCHDOG_TEST_TARGET", PROJECT_ROOT / "claude-watchdog")).resolve()
IDLE_SECONDS = 1.5
POLL_SECONDS = 0.1
PROCESS_TIMEOUT = 8.0
KEEPALIVE_INTERVAL = 0.05
ADMISSION_PATTERN = re.compile(
    r"(?:(?:admitted|adopted|discovered|added).*\bnew\b.*(?:session|activity target)"
    r"|\bnew\b.*(?:session|activity target).*(?:admitted|adopted|discovered|added))",
    re.IGNORECASE,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _rollout_record() -> bytes:
    return (json.dumps({"timestamp": _timestamp()}) + "\n").encode("utf-8")


def _write_rollout(path: Path) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _rollout_record()
    try:
        descriptor = os.open(path, os.O_WRONLY)
    except FileNotFoundError:
        path.write_bytes(data)
    else:
        try:
            os.pwrite(descriptor, data, 0)
            os.ftruncate(descriptor, len(data))
        finally:
            os.close(descriptor)
    return time.time()


class _RolloutKeepalive:
    """Keep a fixed-size synthetic content timestamp current during child startup."""

    def __init__(self, path: Path):
        self.path = path
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stopped = False
        self.last_written = _write_rollout(path)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(KEEPALIVE_INTERVAL):
            self.last_written = _write_rollout(self.path)

    def stop(self) -> float:
        if not self._stopped:
            self._stop.set()
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise AssertionError(f"rollout keepalive did not stop for {self.path}")
            self.last_written = _write_rollout(self.path)
            self._stopped = True
        return self.last_written


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owned_caffeinate_command(pid: int, watchdog_pid: int) -> tuple[bool, str]:
    """Verify a recorded PID is the caffeinate tied to our watchdog process."""
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    command = result.stdout.strip()
    try:
        argv = shlex.split(command)
    except ValueError:
        return False, command
    owns_watchdog = any(
        value == "-w" and index + 1 < len(argv) and argv[index + 1] == str(watchdog_pid)
        for index, value in enumerate(argv)
    )
    return bool(argv and argv[0] == "/usr/bin/caffeinate" and owns_watchdog), command


class _RunningWatchdog:
    def __init__(self, process: subprocess.Popen[str], caffeinate_pid_file: Path):
        self.process = process
        self.caffeinate_pid_file = caffeinate_pid_file
        self.lines: list[str] = []
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_output, daemon=True)
        self._output_closed = False
        self._reader.start()

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                stripped = line.rstrip("\n")
                self.lines.append(stripped)
                self._lines.put(stripped)
        finally:
            self._lines.put(None)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def wait_for(self, pattern: str | re.Pattern[str], timeout: float = PROCESS_TIMEOUT) -> str:
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"timed out waiting for {compiled.pattern!r}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                self.fail(f"timed out waiting for {compiled.pattern!r}")
            if line is None:
                self.fail(
                    f"watchdog exited with {self.process.poll()} before "
                    f"{compiled.pattern!r} appeared"
                )
            if compiled.search(line):
                return line

    def wait_for_caffeinate_pid(self, timeout: float = 3.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self.caffeinate_pid_file.read_text(encoding="utf-8").strip()
                pid = int(value)
            except (FileNotFoundError, OSError, ValueError):
                time.sleep(0.02)
                continue
            return pid
        self.fail("caffeinate shim did not record its PID")

    def wait(self, timeout: float = PROCESS_TIMEOUT) -> int:
        try:
            result = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail("watchdog did not exit naturally")
        self.close_output()
        return result

    def close_output(self) -> None:
        if self._output_closed:
            return
        self._reader.join(timeout=1.0)
        if self._reader.is_alive():
            self.fail("watchdog output reader did not finish")
        if self.process.stdout is not None:
            self.process.stdout.close()
        self._output_closed = True

    def fail(self, message: str) -> None:
        raise AssertionError(f"{message}\n--- watchdog output ---\n{self.output}")


class IsolatedWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="watchdog-isolated-")
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.codex_home = self.root / "codex-home"
        self.xdg_data_home = self.root / "xdg-data"
        self.opencode_db = self.root / "opencode" / "never-used.db"
        self.shims = self.root / "shims"
        self.caffeinate_pid_file = self.root / "caffeinate.pid"
        self.pmset_attempt_file = self.root / "pmset-attempted"
        self.running: list[_RunningWatchdog] = []
        self.keepalives: list[_RolloutKeepalive] = []
        for directory in (self.home, self.codex_home, self.xdg_data_home, self.shims):
            directory.mkdir(parents=True)
        self._write_shims()

    def tearDown(self) -> None:
        cleanup_errors: list[str] = []
        for keepalive in self.keepalives:
            try:
                keepalive.stop()
            except AssertionError as exc:
                cleanup_errors.append(str(exc))
        for running in reversed(self.running):
            if running.process.poll() is None:
                running.process.terminate()
                try:
                    running.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    running.process.kill()
                    running.process.wait(timeout=3)
            try:
                pid = int(running.caffeinate_pid_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                pid = None
            if pid is not None and _pid_exists(pid):
                owned, command = _owned_caffeinate_command(pid, running.process.pid)
                if not owned:
                    if _pid_exists(pid):
                        cleanup_errors.append(
                            f"refused to signal PID {pid}; "
                            f"ownership mismatch: {command!r}"
                        )
                else:
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while _pid_exists(pid) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    if _pid_exists(pid):
                        still_owned, command = _owned_caffeinate_command(
                            pid, running.process.pid
                        )
                        if still_owned:
                            os.kill(pid, signal.SIGKILL)
                        else:
                            cleanup_errors.append(
                                f"refused SIGKILL for PID {pid}; ownership changed: "
                                f"{command!r}"
                            )
            try:
                running.close_output()
            except AssertionError as exc:
                cleanup_errors.append(str(exc))
        self._temporary.cleanup()
        if cleanup_errors:
            self.fail("; ".join(cleanup_errors))

    def _write_shims(self) -> None:
        caffeinate = self.shims / "caffeinate"
        caffeinate.write_text(
            "#!/bin/sh\n"
            ": \"${WATCHDOG_CAFFEINATE_PID_FILE:?missing PID file}\"\n"
            "if [ -n \"${WATCHDOG_CAFFEINATE_START_DELAY:-}\" ]; then\n"
            "  sleep \"$WATCHDOG_CAFFEINATE_START_DELAY\"\n"
            "fi\n"
            "printf '%s\\n' \"$$\" > \"$WATCHDOG_CAFFEINATE_PID_FILE\"\n"
            "exec /usr/bin/caffeinate \"$@\"\n",
            encoding="utf-8",
        )
        caffeinate.chmod(0o700)

        pmset = self.shims / "pmset"
        pmset.write_text(
            "#!/bin/sh\n"
            ": \"${WATCHDOG_PMSET_ATTEMPT_FILE:?missing sentinel file}\"\n"
            "printf '%s\\n' \"$*\" >> \"$WATCHDOG_PMSET_ATTEMPT_FILE\"\n"
            "exit 97\n",
            encoding="utf-8",
        )
        pmset.chmod(0o700)

    def _launch(
        self,
        discovery: str,
        idle_seconds: float = IDLE_SECONDS,
        caffeinate_start_delay: float = 0,
        source: str = "codex",
    ) -> _RunningWatchdog:
        env = os.environ.copy()
        env.pop("WATCHDOG_TARGET", None)
        env.pop("WATCHDOG_TEST_TARGET", None)
        env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "XDG_DATA_HOME": str(self.xdg_data_home),
                "OPENCODE_DB": str(self.opencode_db),
                "PATH": os.pathsep.join((str(self.shims), "/usr/bin", "/bin")),
                "WATCHDOG_CAFFEINATE_PID_FILE": str(self.caffeinate_pid_file),
                "WATCHDOG_CAFFEINATE_START_DELAY": str(caffeinate_start_delay),
                "WATCHDOG_PMSET_ATTEMPT_FILE": str(self.pmset_attempt_file),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        argv = [
            sys.executable,
            str(TARGET),
            str(idle_seconds / 60),
            "--source",
            source,
            "--session-discovery",
            discovery,
            "--user-idle-minutes",
            "0",
            "--select-window",
            "30",
            "--poll",
            str(POLL_SECONDS),
            "--dry-run",
        ]
        process = subprocess.Popen(
            argv,
            cwd=TARGET.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        running = _RunningWatchdog(process, self.caffeinate_pid_file)
        self.running.append(running)
        return running

    def _start_keepalive(self, path: Path) -> _RolloutKeepalive:
        keepalive = _RolloutKeepalive(path)
        keepalive.start()
        self.keepalives.append(keepalive)
        return keepalive

    def _wait_for_holding_handshake(self, running: _RunningWatchdog, source: str = "codex") -> int:
        running.wait_for(r"watching 1 .*activity target")
        caffeinate_pid = running.wait_for_caffeinate_pid()
        running.wait_for(r"source guards:.*" + re.escape(source) + "=holding")
        return caffeinate_pid

    def _assert_no_pmset_attempt(self) -> None:
        self.assertFalse(
            self.pmset_attempt_file.exists(),
            "dry-run invoked the fail-closed pmset sentinel",
        )

    def _assert_child_released(self, running: _RunningWatchdog) -> None:
        pid = running.wait_for_caffeinate_pid()
        deadline = time.monotonic() + 3
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_exists(pid), f"owned caffeinate PID {pid} survived exit")
        self._assert_no_pmset_attempt()
        print(json.dumps({
            "test": self._testMethodName,
            "watchdog_pid": running.process.pid,
            "caffeinate_pid": pid,
            "caffeinate_released": True,
            "pmset_invoked": False,
            "admission_lines": [line for line in running.lines if ADMISSION_PATTERN.search(line)],
        }), flush=True)

    def test_live_mode_admits_new_rollout_once_and_extends_hold(self) -> None:
        first = self.codex_home / "sessions" / "2026" / "09" / "05" / "rollout-first.jsonl"
        keepalive = self._start_keepalive(first)
        running = self._launch("live", caffeinate_start_delay=2.0)
        self._wait_for_holding_handshake(running)
        first_written = keepalive.stop()

        time.sleep(0.6)
        second = self.codex_home / "sessions" / "2026" / "09" / "05" / "rollout-second.jsonl"
        _write_rollout(second)
        running.wait_for(ADMISSION_PATTERN)

        original_quiet_deadline = first_written + IDLE_SECONDS + 0.15
        if (remaining := original_quiet_deadline - time.time()) > 0:
            time.sleep(remaining)
        self.assertIsNone(
            running.process.poll(),
            "live watchdog exited at the original session's quiet point",
        )
        self.assertEqual(running.wait(), 0, running.output)
        admission_lines = [line for line in running.lines if ADMISSION_PATTERN.search(line)]
        self.assertEqual(len(admission_lines), 1, running.output)
        self.assertIn(str(second), running.output)
        self._assert_child_released(running)

    def test_frozen_mode_ignores_rollout_created_after_startup(self) -> None:
        first = self.codex_home / "sessions" / "rollout-frozen-first.jsonl"
        keepalive = self._start_keepalive(first)
        running = self._launch("frozen")
        self._wait_for_holding_handshake(running)
        keepalive.stop()

        second = self.codex_home / "sessions" / "rollout-frozen-second.jsonl"
        _write_rollout(second)
        self.assertEqual(running.wait(), 0, running.output)
        self.assertFalse(any(ADMISSION_PATTERN.search(line) for line in running.lines))
        self.assertNotIn(str(second), running.output)
        self._assert_child_released(running)

    def test_live_profile_discovery_uses_launch_configuration(self) -> None:
        projects = self.home / "work-data" / "projects"
        first = projects / "project" / "first.jsonl"
        keepalive = self._start_keepalive(first)
        config = self.home / ".config" / "claude-watchdog" / "profiles.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "version": 1, "claude_profiles": [
                {"id": "work", "label": "Work", "projects_dir": str(projects)},
            ],
        }), encoding="utf-8")
        running = self._launch("live", source="claude")
        self._wait_for_holding_handshake(running, "claude")

        replacement = self.home / "other-data" / "projects"
        config.write_text(json.dumps({
            "version": 1, "claude_profiles": [
                {"id": "other", "projects_dir": str(replacement)},
            ],
        }), encoding="utf-8")
        ignored = replacement / "project" / "ignored.jsonl"
        _write_rollout(ignored)
        second = projects / "project" / "second.jsonl"
        _write_rollout(second)
        running.wait_for(ADMISSION_PATTERN)
        keepalive.stop()

        self.assertEqual(running.wait(), 0, running.output)
        self.assertIn(str(second), running.output)
        self.assertNotIn(str(ignored), running.output)
        self.assertEqual(sum(bool(ADMISSION_PATTERN.search(line)) for line in running.lines), 1)
        self._assert_child_released(running)

    def test_sigterm_releases_only_the_watchdogs_caffeinate_child(self) -> None:
        rollout = self.codex_home / "sessions" / "rollout-signal.jsonl"
        _write_rollout(rollout)
        running = self._launch("live", idle_seconds=60)
        caffeinate_pid = self._wait_for_holding_handshake(running)
        self.assertTrue(_pid_exists(caffeinate_pid))

        running.process.send_signal(signal.SIGTERM)
        running.process.wait(timeout=3)
        running.close_output()
        self._assert_child_released(running)


if __name__ == "__main__":
    if not TARGET.is_file():
        raise SystemExit(f"test watchdog target not found: {TARGET}")
    unittest.main(verbosity=2)
