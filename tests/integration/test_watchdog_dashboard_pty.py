#!/usr/bin/env python3
"""Isolated PTY integration checks for the claude-watchdog dashboard.

The suite creates synthetic Codex rollouts and metadata under a temporary home.
It never reads the caller's sessions, never invokes a real sleep command, and
signals only the watchdog process it spawned or a recorded caffeinate child
whose command line proves that it belongs to that watchdog.

Successful and failed runs leave ANSI transcripts plus a compact JSON evidence
file under ``.omx/artifacts/dashboard-pty`` for review.
"""

from __future__ import annotations

import codecs
import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TARGET = Path(os.environ.get("WATCHDOG_TEST_TARGET", PROJECT_ROOT / "claude-watchdog")).resolve()
ARTIFACT_DIR = PROJECT_ROOT / ".omx" / "artifacts" / "dashboard-pty" / (
    "source" if TARGET == PROJECT_ROOT / "claude-watchdog" else "installed"
)
POLL_SECONDS = 0.2
WAIT_SECONDS = 10.0
ALTERNATE_SCREEN_EXIT = b"\x1b[?1049l"
SGR_SEQUENCE = re.compile(rb"\x1b\[[0-9;]*m")
EXPLICIT_COLOR = re.compile(
    rb"\x1b\[(?:3[0-7]|4[0-7]|9[0-7]|10[0-7]|38(?:;[0-9]+)+|48(?:;[0-9]+)+)m"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str:
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _owned_caffeinate(pid: int, watchdog_pid: int) -> tuple[bool, str]:
    """Return whether *pid* is caffeinate waiting on our exact watchdog PID."""
    command = _process_command(pid)
    try:
        argv = shlex.split(command)
    except ValueError:
        return False, command
    waits_on_watchdog = any(
        arg == "-w"
        and index + 1 < len(argv)
        and argv[index + 1] == str(watchdog_pid)
        for index, arg in enumerate(argv)
    )
    return bool(
        argv
        and argv[0] == "/usr/bin/caffeinate"
        and waits_on_watchdog
    ), command


def _set_pty_size(fd: int, rows: int, columns: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


class _AnsiScreen:
    """A deliberately small ANSI screen model for curses integration assertions."""

    def __init__(self, rows: int, columns: int):
        self.rows = rows
        self.columns = columns
        self.grid = [[" "] * columns for _ in range(rows)]
        self.row = 0
        self.column = 0
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.saved_cursor = (0, 0)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "text"
        self._sequence = ""

    def resize(self, rows: int, columns: int) -> None:
        resized = [[" "] * columns for _ in range(rows)]
        for row in range(min(rows, self.rows)):
            for column in range(min(columns, self.columns)):
                resized[row][column] = self.grid[row][column]
        self.rows = rows
        self.columns = columns
        self.grid = resized
        self.row = min(self.row, rows - 1)
        self.column = min(self.column, columns - 1)
        self.scroll_top = 0
        self.scroll_bottom = rows - 1

    def feed(self, data: bytes) -> None:
        for character in self._decoder.decode(data):
            if self._state == "text":
                self._text(character)
            elif self._state == "escape":
                self._escape(character)
            elif self._state == "csi":
                self._csi(character)
            elif self._state == "osc":
                if character == "\a":
                    self._state = "text"
                elif character == "\x1b":
                    self._state = "osc_escape"
            elif self._state == "osc_escape":
                self._state = "text" if character == "\\" else "osc"
            elif self._state == "charset":
                self._state = "text"

    def _text(self, character: str) -> None:
        if character == "\x1b":
            self._state = "escape"
        elif character == "\r":
            self.column = 0
        elif character in ("\n", "\v", "\f"):
            self._line_feed()
        elif character == "\b":
            self.column = max(0, self.column - 1)
        elif character == "\t":
            self.column = min(self.columns - 1, ((self.column // 8) + 1) * 8)
        elif character >= " " and character != "\x7f":
            self.grid[self.row][self.column] = character
            if self.column < self.columns - 1:
                self.column += 1

    def _escape(self, character: str) -> None:
        if character == "[":
            self._sequence = ""
            self._state = "csi"
        elif character == "]":
            self._state = "osc"
        elif character in "()":
            self._state = "charset"
        elif character == "7":
            self.saved_cursor = (self.row, self.column)
            self._state = "text"
        elif character == "8":
            self.row, self.column = self.saved_cursor
            self._state = "text"
        elif character == "D":
            self._line_feed()
            self._state = "text"
        elif character == "M":
            self._reverse_index()
            self._state = "text"
        else:
            self._state = "text"

    def _csi(self, character: str) -> None:
        self._sequence += character
        if "@" <= character <= "~":
            self._apply_csi(character, self._sequence[:-1])
            self._state = "text"

    @staticmethod
    def _numbers(parameters: str, default: int = 1) -> list[int]:
        clean = parameters.lstrip("?<>=!")
        return [int(value) if value else default for value in clean.split(";")]

    def _apply_csi(self, final: str, parameters: str) -> None:
        try:
            values = self._numbers(parameters)
        except ValueError:
            return
        amount = values[0] if values else 1
        if final in "Hf":
            row = (values[0] if values else 1) - 1
            column = (values[1] if len(values) > 1 else 1) - 1
            self.row = min(max(row, 0), self.rows - 1)
            self.column = min(max(column, 0), self.columns - 1)
        elif final == "A":
            self.row = max(0, self.row - amount)
        elif final in "Be":
            self.row = min(self.rows - 1, self.row + amount)
        elif final in "Ca":
            self.column = min(self.columns - 1, self.column + amount)
        elif final == "D":
            self.column = max(0, self.column - amount)
        elif final in "G`":
            self.column = min(max(amount - 1, 0), self.columns - 1)
        elif final == "d":
            self.row = min(max(amount - 1, 0), self.rows - 1)
        elif final == "J":
            mode = self._numbers(parameters, default=0)[0]
            if mode in (2, 3):
                self.grid = [[" "] * self.columns for _ in range(self.rows)]
                self.row = self.column = 0
            elif mode == 0:
                self._erase_to_end_of_screen()
        elif final == "K":
            mode = self._numbers(parameters, default=0)[0]
            if mode == 0:
                for column in range(self.column, self.columns):
                    self.grid[self.row][column] = " "
            elif mode == 1:
                for column in range(0, self.column + 1):
                    self.grid[self.row][column] = " "
            elif mode == 2:
                self.grid[self.row] = [" "] * self.columns
        elif final == "X":
            for column in range(self.column, min(self.columns, self.column + amount)):
                self.grid[self.row][column] = " "
        elif final == "r":
            margins = self._numbers(parameters, default=0)
            top = (margins[0] or 1) - 1
            bottom = (margins[1] if len(margins) > 1 and margins[1] else self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.scroll_top = top
                self.scroll_bottom = bottom
                self.row = self.column = 0
        elif final == "s":
            self.saved_cursor = (self.row, self.column)
        elif final == "u":
            self.row, self.column = self.saved_cursor
        elif final in "h" and parameters.startswith("?1049"):
            self.grid = [[" "] * self.columns for _ in range(self.rows)]
            self.row = self.column = 0

    def _erase_to_end_of_screen(self) -> None:
        for column in range(self.column, self.columns):
            self.grid[self.row][column] = " "
        for row in range(self.row + 1, self.rows):
            self.grid[row] = [" "] * self.columns

    def _line_feed(self) -> None:
        if self.row == self.scroll_bottom:
            del self.grid[self.scroll_top]
            self.grid.insert(self.scroll_bottom, [" "] * self.columns)
        else:
            self.row = min(self.rows - 1, self.row + 1)

    def _reverse_index(self) -> None:
        if self.row == self.scroll_top:
            del self.grid[self.scroll_bottom]
            self.grid.insert(self.scroll_top, [" "] * self.columns)
        else:
            self.row = max(0, self.row - 1)

    @property
    def text(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.grid)


class _PtyWatchdog:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        master_fd: int,
        screen: _AnsiScreen,
        caffeinate_pid_file: Path,
        transcript_path: Path,
        mode: str,
        term: str,
    ):
        self.process = process
        self.master_fd = master_fd
        self.screen = screen
        self.caffeinate_pid_file = caffeinate_pid_file
        self.transcript_path = transcript_path
        self.mode = mode
        self.term = term
        self.transcript = bytearray()
        self.caffeinate_pid: int | None = None
        self.closed = False

    @property
    def decoded(self) -> str:
        return self.transcript.decode("utf-8", errors="replace")

    def read_available(self, timeout: float = 0.05) -> bytes:
        chunks: list[bytes] = []
        ready, _, _ = select.select([self.master_fd], [], [], timeout)
        while ready:
            try:
                chunk = os.read(self.master_fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
            self.transcript.extend(chunk)
            self.screen.feed(chunk)
            ready, _, _ = select.select([self.master_fd], [], [], 0)
        return b"".join(chunks)

    def wait_for_screen(
        self,
        predicate: Callable[[str], bool],
        description: str,
        timeout: float = WAIT_SECONDS,
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read_available()
            current = self.screen.text
            if predicate(current):
                return current
            if self.process.poll() is not None:
                break
        self.fail(f"timed out waiting for screen: {description}")

    def wait_for_output(self, pattern: str, timeout: float = WAIT_SECONDS) -> str:
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read_available()
            if compiled.search(self.decoded):
                return self.decoded
            if self.process.poll() is not None:
                break
        self.fail(f"timed out waiting for output pattern {pattern!r}")

    def send(self, data: bytes) -> None:
        if self.process.poll() is not None:
            self.fail(f"cannot send input; watchdog exited {self.process.returncode}")
        os.write(self.master_fd, data)

    def resize(self, rows: int, columns: int) -> None:
        _set_pty_size(self.master_fd, rows, columns)
        self.screen.resize(rows, columns)
        self.process.send_signal(signal.SIGWINCH)

    def wait_for_caffeinate_pid(self, timeout: float = 3.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pid = int(self.caffeinate_pid_file.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, OSError, ValueError):
                time.sleep(0.02)
                continue
            self.caffeinate_pid = pid
            return pid
        self.fail("caffeinate shim did not record its PID")

    def wait(self, timeout: float = WAIT_SECONDS) -> int:
        deadline = time.monotonic() + timeout
        return_code = self.process.poll()
        while return_code is None and time.monotonic() < deadline:
            self.read_available(0.05)
            return_code = self.process.poll()
        if return_code is None:
            self.fail("watchdog did not exit in time")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not self.read_available(0.02):
                break
        return return_code

    def fail(self, message: str) -> None:
        raise AssertionError(
            f"{message}\n--- current screen ---\n{self.screen.text}"
            f"\n--- transcript tail ---\n{self.decoded[-6000:]}"
        )

    def save_transcript(self) -> None:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path.write_bytes(bytes(self.transcript))

    def close(self) -> None:
        if self.closed:
            return
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        self.closed = True


class DashboardPtyTests(unittest.TestCase):
    evidence: list[dict[str, object]] = []

    @classmethod
    def tearDownClass(cls) -> None:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        evidence_path = ARTIFACT_DIR / "evidence.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "generated_at": _now(),
                    "target": str(TARGET),
                    "synthetic_fixture_only": True,
                    "tests": cls.evidence,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="watchdog-dashboard-pty-")
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.codex_home = self.root / "codex-home"
        self.shims = self.root / "shims"
        self.xdg_data_home = self.root / "xdg-data"
        self.pmset_attempt_file = self.root / "pmset-attempted"
        self.running: list[_PtyWatchdog] = []
        for path in (self.home, self.codex_home, self.shims, self.xdg_data_home):
            path.mkdir(parents=True)
        self._write_shims()
        self._seed_codex_sessions()

    def tearDown(self) -> None:
        errors: list[str] = []
        for running in reversed(self.running):
            running.read_available(0)
            running.save_transcript()
            if running.process.poll() is None:
                running.process.send_signal(signal.SIGTERM)
                try:
                    running.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    running.process.kill()
                    running.process.wait(timeout=2)
            running.read_available(0)
            running.save_transcript()
            try:
                self._release_owned_caffeinate(running)
            except AssertionError as exc:
                errors.append(str(exc))
            running.close()
        if self.pmset_attempt_file.exists():
            errors.append(
                "pmset blocker was invoked: "
                + self.pmset_attempt_file.read_text(encoding="utf-8", errors="replace")
            )
        self._temporary.cleanup()
        if errors:
            self.fail("; ".join(errors))

    def _write_shims(self) -> None:
        caffeinate = self.shims / "caffeinate"
        caffeinate.write_text(
            "#!/bin/sh\n"
            ": \"${WATCHDOG_CAFFEINATE_PID_FILE:?missing PID file}\"\n"
            "printf '%s\\n' \"$$\" > \"$WATCHDOG_CAFFEINATE_PID_FILE\"\n"
            "exec /usr/bin/caffeinate \"$@\"\n",
            encoding="utf-8",
        )
        caffeinate.chmod(0o700)

        pmset = self.shims / "pmset"
        pmset.write_text(
            "#!/bin/sh\n"
            ": \"${WATCHDOG_PMSET_ATTEMPT_FILE:?missing sentinel}\"\n"
            "printf '%s\\n' \"$*\" >> \"$WATCHDOG_PMSET_ATTEMPT_FILE\"\n"
            "exit 97\n",
            encoding="utf-8",
        )
        pmset.chmod(0o700)

        ioreg = self.shims / "ioreg"
        ioreg.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '  \"HIDIdleTime\" = 999999999999'\n",
            encoding="utf-8",
        )
        ioreg.chmod(0o700)

    def _seed_codex_sessions(self) -> None:
        session_dir = self.codex_home / "sessions" / "2026" / "09" / "05"
        session_dir.mkdir(parents=True)
        records = (
            (
                "synthetic-alpha",
                session_dir / "rollout-synthetic-alpha.jsonl",
                "Alpha overnight audit",
                "gpt-5.6-sol",
                "high",
            ),
            (
                "synthetic-beta",
                session_dir / "rollout-synthetic-beta.jsonl",
                "Beta dashboard build",
                "gpt-5.6-terra",
                "medium",
            ),
        )
        timestamp = _now()
        for thread_id, rollout, _title, model, effort in records:
            events = [
                {
                    "timestamp": timestamp,
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "timestamp": timestamp,
                        "cwd": "/tmp/synthetic-watchdog-project",
                        "originator": "Codex Desktop",
                        "source": "vscode",
                        "model_provider": "openai",
                    },
                },
                {
                    "timestamp": timestamp,
                    "type": "turn_context",
                    "payload": {
                        "model": model,
                        "effort": effort,
                        "reasoning_effort": effort,
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

        database = sqlite3.connect(self.codex_home / "state_5.sqlite")
        try:
            database.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    model TEXT,
                    reasoning_effort TEXT,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER
                )
                """
            )
            epoch = int(time.time())
            for thread_id, rollout, title, model, effort in records:
                database.execute(
                    """
                    INSERT INTO threads (
                        id, rollout_path, created_at, updated_at, source,
                        model_provider, cwd, title, model, reasoning_effort,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        str(rollout),
                        epoch,
                        epoch,
                        "vscode",
                        "openai",
                        "/tmp/synthetic-watchdog-project",
                        title,
                        model,
                        effort,
                        epoch * 1000,
                        epoch * 1000,
                    ),
                )
            database.commit()
        finally:
            database.close()

    def _age_session_activity(self, seconds: float) -> None:
        """Give every synthetic session an old persisted-content timestamp."""
        timestamp = datetime.fromtimestamp(
            time.time() - seconds, timezone.utc
        ).isoformat()
        for rollout in (self.codex_home / "sessions").rglob("*.jsonl"):
            events = [
                json.loads(line)
                for line in rollout.read_text(encoding="utf-8").splitlines()
            ]
            for event in events:
                event["timestamp"] = timestamp
                payload = event.get("payload")
                if isinstance(payload, dict) and "timestamp" in payload:
                    payload["timestamp"] = timestamp
            rollout.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

    def _launch(
        self,
        display: str,
        *,
        idle_minutes: float = 1.0,
        term: str = "xterm-256color",
        rows: int = 30,
        columns: int = 120,
        no_color: bool = False,
        no_color_environment: bool = False,
    ) -> _PtyWatchdog:
        launch_number = len(self.running) + 1
        pid_file = self.root / f"caffeinate-{launch_number}.pid"
        transcript_path = ARTIFACT_DIR / (
            f"{self._testMethodName}-{launch_number}-{display}-{term}.ansi"
        )
        master_fd, slave_fd = pty.openpty()
        _set_pty_size(slave_fd, rows, columns)
        env = os.environ.copy()
        env.pop("WATCHDOG_TARGET", None)
        env.pop("WATCHDOG_TEST_TARGET", None)
        env.pop("NO_COLOR", None)
        env.update(
            {
                "HOME": str(self.home),
                "CODEX_HOME": str(self.codex_home),
                "XDG_DATA_HOME": str(self.xdg_data_home),
                "OPENCODE_DB": str(self.root / "unused-opencode.db"),
                "PATH": os.pathsep.join((str(self.shims), "/usr/bin", "/bin")),
                "TERM": term,
                "WATCHDOG_CAFFEINATE_PID_FILE": str(pid_file),
                "WATCHDOG_PMSET_ATTEMPT_FILE": str(self.pmset_attempt_file),
                "PYTHONDONTWRITEBYTECODE": "1",
                "LC_ALL": "C.UTF-8",
            }
        )
        if no_color_environment:
            env["NO_COLOR"] = "1"
        argv = [
            sys.executable,
            str(TARGET),
            str(idle_minutes),
            "--source",
            "codex",
            "--session-discovery",
            "live",
            "--user-idle-minutes",
            "0",
            "--select-window",
            "60",
            "--poll",
            str(POLL_SECONDS),
            "--dry-run",
            "--display",
            display,
        ]
        if no_color:
            argv.append("--no-color")
        process = subprocess.Popen(
            argv,
            cwd=TARGET.parent,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)
        running = _PtyWatchdog(
            process,
            master_fd,
            _AnsiScreen(rows, columns),
            pid_file,
            transcript_path,
            display,
            term,
        )
        self.running.append(running)
        return running

    def _assert_alive_with_owned_hold(self, running: _PtyWatchdog) -> int:
        self.assertIsNone(running.process.poll(), running.decoded)
        pid = running.wait_for_caffeinate_pid()
        owned, command = _owned_caffeinate(pid, running.process.pid)
        self.assertTrue(owned, f"unexpected caffeinate command: {command!r}")
        self.assertTrue(_pid_exists(pid), f"caffeinate PID {pid} is not alive")
        return pid

    def _release_owned_caffeinate(self, running: _PtyWatchdog) -> None:
        pid = running.caffeinate_pid
        if pid is None:
            try:
                pid = int(running.caffeinate_pid_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                return
        deadline = time.monotonic() + 3
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if not _pid_exists(pid):
            return
        owned, command = _owned_caffeinate(pid, running.process.pid)
        if not owned:
            raise AssertionError(
                f"refused to signal recorded PID {pid}; ownership mismatch: {command!r}"
            )
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _pid_exists(pid):
            owned, command = _owned_caffeinate(pid, running.process.pid)
            if not owned:
                raise AssertionError(
                    f"refused SIGKILL for PID {pid}; ownership changed: {command!r}"
                )
            os.kill(pid, signal.SIGKILL)

    def _assert_caffeinate_released(self, running: _PtyWatchdog) -> None:
        pid = running.caffeinate_pid
        self.assertIsNotNone(pid)
        assert pid is not None
        deadline = time.monotonic() + 3
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_pid_exists(pid), f"owned caffeinate PID {pid} survived exit")
        self.assertFalse(self.pmset_attempt_file.exists(), "pmset blocker was invoked")

    def _exit_report(self, running: _PtyWatchdog) -> bytes:
        self.assertIn(ALTERNATE_SCREEN_EXIT, running.transcript)
        after_dashboard = running.transcript.rsplit(ALTERNATE_SCREEN_EXIT, 1)[1]
        marker = b"CLAUDE WATCHDOG \xe2\x80\x94 RUN REPORT"
        self.assertIn(marker, after_dashboard)
        report = after_dashboard[after_dashboard.index(marker):]
        self.assertNotIn(
            b"\x1b",
            SGR_SEQUENCE.sub(b"", report),
            "exit report contains a non-SGR terminal control sequence",
        )
        self.assertNotIn(b"q exit safely", report)
        self.assertNotRegex(report, rb"showing\s+\d+\s*/\s*\d+")
        return report

    def _record(self, running: _PtyWatchdog, **facts: object) -> None:
        self.evidence.append(
            {
                "test": self._testMethodName,
                "display": running.mode,
                "term": running.term,
                "watchdog_pid": running.process.pid,
                "caffeinate_pid": running.caffeinate_pid,
                "return_code": running.process.returncode,
                "transcript": str(running.transcript_path.relative_to(PROJECT_ROOT)),
                "transcript_bytes": len(running.transcript),
                "pmset_invoked": self.pmset_attempt_file.exists(),
                **facts,
            }
        )

    def test_auto_dashboard_filters_and_controls_without_changing_hold(self) -> None:
        incremental = _AnsiScreen(30, 120)
        incremental.feed(
            b"\x1b[29;1Hshowing 2 / 2 - provider all - sort recent"
            b"\x1b[30;41H\x1b[A\b\b\b\btitle\x1b[K"
        )
        self.assertIn("sort title", incremental.text)

        running = self._launch("auto")
        screen = running.wait_for_screen(
            lambda text: all(
                expected.lower() in text.lower()
                for expected in (
                    "CLAUDE WATCHDOG",
                    "Alpha overnight audit",
                    "Beta dashboard build",
                    "gpt-5.6-sol",
                    "high",
                    "holding",
                )
            ),
            "dashboard task, model, effort, and guard labels",
        )
        self.assertIn("persisted activity", screen.lower())
        caffeinate_pid = self._assert_alive_with_owned_hold(running)

        running.send(b"/Beta dashboard\r")
        filtered = running.wait_for_screen(
            lambda text: (
                "Beta dashboard build" in text
                and "Alpha overnight audit" not in text
                and re.search(r"showing\s+1 rows.*2 watch targets", text, re.IGNORECASE) is not None
            ),
            "one filtered row with the other row erased",
        )
        self.assertIn("Beta dashboard build", filtered)
        self.assertIsNone(running.process.poll(), "filtering changed watchdog lifecycle")
        self.assertTrue(_pid_exists(caffeinate_pid), "filtering released the sleep hold")

        running.send(b"q")
        self.assertEqual(running.wait(), 130, running.decoded)
        report = self._exit_report(running)
        self.assertIn(b"STOPPED", report)
        self.assertIn(b"FINAL SESSIONS", report)
        self.assertIn(b"Alpha overnight audit", report)
        self.assertIn(b"Beta dashboard build", report)
        self.assertIsNotNone(
            EXPLICIT_COLOR.search(report), "colored dashboard produced a monochrome report"
        )
        self.assertNotRegex(report, rb"provider\s+all\s+.*sort")
        self.assertIn(b"\x1b[?1049h", running.transcript)
        self._assert_caffeinate_released(running)
        self._record(
            running,
            dashboard_selected_automatically=True,
            metadata_visible=True,
            filter_display_only=True,
            filtered_exit_report_includes_all_sessions=True,
            exit_report_outcome="STOPPED",
            exit_report_colored=True,
            alternate_screen_restored=True,
            caffeinate_released=True,
        )

    def test_dashboard_controls_do_not_change_hold(self) -> None:
        running = self._launch("dashboard")
        running.wait_for_screen(
            lambda text: "Alpha overnight audit" in text and "Beta dashboard build" in text,
            "dashboard before exercising controls",
        )
        caffeinate_pid = self._assert_alive_with_owned_hold(running)
        running.send(b"t")
        running.wait_for_screen(lambda text: "flat · sort" in text, "tree toggle selects flat view")
        running.send(b"t")
        running.wait_for_screen(lambda text: "tree · sort" in text, "tree toggle restores hierarchy")
        running.send(b"s")
        running.wait_for_screen(
            lambda text: "sort title" in text.lower(), "sort key updates the footer"
        )
        running.send(b"f")
        running.wait_for_screen(
            lambda text: "No watched sessions match this filter" in text,
            "provider alias can produce an empty presentation filter",
        )
        self.assertIsNone(running.process.poll(), "provider filtering exited watchdog")
        self.assertTrue(_pid_exists(caffeinate_pid), "provider filtering released hold")
        running.send(b"c")
        running.wait_for_screen(
            lambda text: "Alpha overnight audit" in text and "Beta dashboard build" in text,
            "clear restores rows after provider filtering",
        )
        # ncurses enables application cursor mode (DECCKM) on this PTY.
        running.send(b"\x1bOB")
        def selected_title(text, title):
            return any("▸" in line and title in line for line in text.splitlines())

        running.wait_for_screen(
            lambda text: selected_title(text, "Beta dashboard build"),
            "Down selects Beta through redraw",
        )
        for key, title in ((b"k", "Alpha overnight audit"),
                           (b"j", "Beta dashboard build"),
                           (b"\x1bOA", "Alpha overnight audit")):
            running.send(key)
            running.wait_for_screen(
                lambda text, title=title: selected_title(text, title),
                "navigation changes the selected row",
            )
        running.send(b"\r")
        detail = running.wait_for_screen(
            lambda text: "selected: started" in text.lower(),
            "arrow selection and metadata detail panel",
        )
        self.assertRegex(detail, r"gpt-5\.6-(?:sol|terra)\s*/\s*(?:high|medium)")

        running.send(b"q")
        self.assertEqual(running.wait(), 130, running.decoded)
        self.assertIn(b"STOPPED", self._exit_report(running))
        self._assert_caffeinate_released(running)
        self._record(
            running,
            dashboard_selected_automatically=True,
            metadata_visible=True,
            filter_display_only=True,
            controls_survived=["c", "s", "f", "down", "enter"],
            alternate_screen_restored=True,
            caffeinate_released=True,
        )

    def test_successful_dry_run_prints_report_before_sleep_decision(self) -> None:
        self._age_session_activity(2)
        running = self._launch("dashboard", idle_minutes=0.01)
        self.assertEqual(running.wait(), 0, running.decoded)
        report = self._exit_report(running)
        self.assertIn(b"DRY RUN", report)
        self.assertIn(b"FINAL SESSIONS", report)
        self.assertIn(b"Alpha overnight audit", report)
        self.assertIn(b"Beta dashboard build", report)
        sleep_log = b"[dry-run] would run: pmset sleepnow"
        self.assertIn(sleep_log, report)
        self.assertLess(
            running.decoded.index("CLAUDE WATCHDOG \u2014 RUN REPORT"),
            running.decoded.index(sleep_log.decode()),
        )
        self.assertFalse(self.pmset_attempt_file.exists(), "pmset blocker was invoked")
        self._record(
            running,
            exit_report_outcome="DRY RUN",
            exit_report_before_force_sleep=True,
            all_final_sessions_visible=True,
            pmset_invoked=False,
            caffeinate_released_or_not_yet_execed=True,
        )

    def test_dashboard_survives_narrow_resize_and_sigwinch(self) -> None:
        running = self._launch("dashboard", rows=30, columns=120)
        running.wait_for_screen(
            lambda text: "CLAUDE WATCHDOG" in text and "Beta dashboard build" in text,
            "wide dashboard",
        )
        self._assert_alive_with_owned_hold(running)
        before_resize = len(running.transcript)
        running.resize(18, 68)
        resized = running.wait_for_screen(
            lambda text: len(running.transcript) > before_resize
            and "CLAUDE WATCHDOG" in text
            and ("Beta dashboard" in text or "Alpha overnight" in text),
            "compact dashboard after SIGWINCH",
        )
        self.assertLessEqual(max(map(len, resized.splitlines()), default=0), 68)
        self.assertIsNone(running.process.poll(), running.decoded)
        self.assertGreater(len(running.transcript), before_resize)
        running.send(b"\x1b[B\r")
        time.sleep(0.1)
        running.read_available()
        self.assertIsNone(running.process.poll(), running.decoded)
        running.process.send_signal(signal.SIGTERM)
        self.assertEqual(running.wait(), 130, running.decoded)
        self.assertIn(b"STOPPED", self._exit_report(running))
        self._assert_caffeinate_released(running)
        self._record(
            running,
            resized_from="120x30",
            resized_to="68x18",
            sigwinch_survived=True,
            selection_and_detail_survived=True,
            sigterm_restored_terminal=True,
            caffeinate_released=True,
        )

    def test_auto_uses_log_fallback_for_dumb_terminal(self) -> None:
        running = self._launch("auto", term="dumb")
        output = running.wait_for_output(r"(?:fallback|falling back|plain log|log display)")
        running.wait_for_output(r"source guards:.*codex=holding")
        self.assertNotIn(b"\x1b[?1049h", running.transcript)
        self.assertRegex(output.lower(), r"(?:fallback|falling back|plain log|log display)")
        self._assert_alive_with_owned_hold(running)
        running.process.send_signal(signal.SIGINT)
        self.assertEqual(running.wait(), 130, running.decoded)
        self._assert_caffeinate_released(running)
        self._record(
            running,
            dumb_terminal_log_fallback=True,
            alternate_screen_entered=False,
            caffeinate_released=True,
        )

    def test_forced_log_and_no_color_dashboard_are_explicit(self) -> None:
        log_run = self._launch("log")
        log_run.wait_for_output(r"source guards:.*codex=holding")
        self.assertNotIn(b"\x1b[?1049h", log_run.transcript)
        self._assert_alive_with_owned_hold(log_run)
        log_run.process.send_signal(signal.SIGINT)
        self.assertEqual(log_run.wait(), 130, log_run.decoded)
        self._assert_caffeinate_released(log_run)
        self._record(
            log_run,
            forced_log_uses_plain_stream=True,
            alternate_screen_entered=False,
            caffeinate_released=True,
        )

        monochrome = self._launch(
            "dashboard", no_color=True, no_color_environment=True
        )
        monochrome.wait_for_screen(
            lambda text: "CLAUDE WATCHDOG" in text and "Beta dashboard build" in text,
            "monochrome dashboard",
        )
        self._assert_alive_with_owned_hold(monochrome)
        monochrome.process.send_signal(signal.SIGINT)
        self.assertEqual(monochrome.wait(), 130, monochrome.decoded)
        self.assertIn(b"\x1b[?1049l", monochrome.transcript)
        self.assertIsNone(
            EXPLICIT_COLOR.search(monochrome.transcript),
            "--no-color/NO_COLOR emitted an explicit foreground or background color",
        )
        monochrome_report = self._exit_report(monochrome)
        self.assertIn(b"STOPPED", monochrome_report)
        self.assertIsNone(
            EXPLICIT_COLOR.search(monochrome_report),
            "--no-color/NO_COLOR colored the exit report",
        )
        self._assert_caffeinate_released(monochrome)
        self._record(
            monochrome,
            no_color_flag=True,
            no_color_environment=True,
            explicit_color_sequences=False,
            ctrl_c_restored_terminal=True,
            caffeinate_released=True,
        )


if __name__ == "__main__":
    if not TARGET.is_file():
        raise SystemExit(f"test watchdog target not found: {TARGET}")
    unittest.main(verbosity=2)
