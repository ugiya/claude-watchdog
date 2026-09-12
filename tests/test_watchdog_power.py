#!/usr/bin/env python3
"""Public power-session contract tests."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from claude_watchdog import models as wd_models
from claude_watchdog import power as wd_power
from claude_watchdog.power import IdleKind, PowerPolicy


class PowerSessionTests(unittest.TestCase):
    def test_open_session_on_macos_holds_with_owned_caffeinate(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch.object(wd_power.subprocess, "Popen", return_value=process) as popen:
            session = wd_power.open_session(PowerPolicy(), platform="darwin")
        popen.assert_called_once_with(
            ["caffeinate", "-is", "-w", str(os.getpid())]
        )
        status = session.poll(check_user_idle=False)
        self.assertEqual(status.keep_awake, "macos")
        self.assertEqual(status.idle.kind, IdleKind.DISABLED)
        session.close()
        process.terminate.assert_called_once_with()

    def test_poll_skips_idle_observer_until_requested(self):
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(wd_power.subprocess, "Popen", return_value=process),
            mock.patch.object(
                wd_power.subprocess,
                "check_output",
                side_effect=AssertionError("idle queried too early"),
            ),
        ):
            session = wd_power.open_session(
                PowerPolicy(user_idle_seconds=300), platform="darwin"
            )
            session.poll(check_user_idle=False)
        session.close()

    def test_unknown_idle_raises_presence_error(self):
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(wd_power.subprocess, "Popen", return_value=process),
            mock.patch.object(
                wd_power.subprocess,
                "check_output",
                side_effect=OSError("ioreg unavailable"),
            ),
        ):
            session = wd_power.open_session(
                PowerPolicy(user_idle_seconds=300), platform="darwin"
            )
            with self.assertRaises(wd_models.PresenceCheckError):
                session.poll(check_user_idle=True)
        session.close()

    def test_dry_run_suspend_does_not_invoke_pmset(self):
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(wd_power.subprocess, "Popen", return_value=process),
            mock.patch.object(wd_power.subprocess, "run") as run,
        ):
            session = wd_power.open_session(
                PowerPolicy(dry_run=True), platform="darwin"
            )
            session.close()
            session.request_suspend()
        run.assert_not_called()

    def test_linux_requires_logind_tools(self):
        with mock.patch("claude_watchdog.power._adapters.logind.shutil.which", return_value=None):
            with self.assertRaisesRegex(wd_models.PowerCommandError, "systemd-inhibit"):
                wd_power.open_session(PowerPolicy(), platform="linux")

    def test_linux_session_acquires_inhibit_and_dry_run_skips_suspend(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0

        def which(name):
            return {
                "systemd-inhibit": "/usr/bin/systemd-inhibit",
                "systemctl": "/usr/bin/systemctl",
                "busctl": "/usr/bin/busctl",
                "loginctl": "/usr/bin/loginctl",
            }.get(name)

        with (
            mock.patch("claude_watchdog.power._adapters.logind.shutil.which", side_effect=which),
            mock.patch(
                "claude_watchdog.power._adapters.logind.subprocess.check_output",
                return_value='s "yes"\n',
            ),
            mock.patch(
                "claude_watchdog.power._adapters.logind.subprocess.Popen",
                return_value=process,
            ) as popen,
            mock.patch("claude_watchdog.power._adapters.logind.subprocess.run") as run,
        ):
            session = wd_power.open_session(PowerPolicy(dry_run=True), platform="linux")
            self.assertEqual(session.poll(check_user_idle=False).keep_awake, "logind")
            session.close()
            session.request_suspend()
        self.assertEqual(popen.call_args.args[0][0], "/usr/bin/systemd-inhibit")
        self.assertIn("--what=idle:sleep", popen.call_args.args[0])
        run.assert_not_called()
        process.terminate.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
