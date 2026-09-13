#!/usr/bin/env python3
"""Public power-session contract tests."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
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


class LogindKeepAwakeTests(unittest.TestCase):
    """Regressions for the inhibitor invocation and its lifetime."""

    @staticmethod
    def _which(name):
        return {
            "systemd-inhibit": "/usr/bin/systemd-inhibit",
            "systemctl": "/usr/bin/systemctl",
            "busctl": "/usr/bin/busctl",
            "loginctl": "/usr/bin/loginctl",
        }.get(name)

    def _acquire(self, process):
        with (
            mock.patch(
                "claude_watchdog.power._adapters.logind.shutil.which",
                side_effect=self._which,
            ),
            mock.patch(
                "claude_watchdog.power._adapters.logind.subprocess.check_output",
                return_value='s "yes"\n',
            ),
            mock.patch(
                "claude_watchdog.power._adapters.logind.subprocess.Popen",
                return_value=process,
            ) as popen,
        ):
            session = wd_power.open_session(PowerPolicy(), platform="linux")
        return session, popen.call_args

    def test_inhibit_argv_contains_only_options_systemd_inhibit_accepts(self):
        """systemd-inhibit takes no --no-ask-password; passing it aborts the hold."""
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        session, call = self._acquire(process)
        argv = call.args[0]
        self.assertNotIn("--no-ask-password", argv)
        for argument in argv[1:]:
            if argument.startswith("--"):
                self.assertIn(
                    argument.split("=", 1)[0],
                    {"--what", "--who", "--why", "--mode"},
                    f"systemd-inhibit does not accept {argument}",
                )
        session.close()

    def test_hold_dies_with_the_watchdog_through_an_owned_pipe(self):
        """`sleep infinity` outlives SIGKILL and strands the machine awake."""
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        session, call = self._acquire(process)
        argv, kwargs = call.args[0], call.kwargs
        self.assertEqual(argv[-1], "cat")
        self.assertNotIn("infinity", argv)
        self.assertIs(kwargs["stdin"], wd_power.subprocess.PIPE)
        self.assertTrue(
            any(f"pid {os.getpid()}" in part for part in argv),
            "the inhibitor reason should name the watchdog it belongs to",
        )
        session.close()

    def test_release_closes_the_pipe_before_signalling(self):
        order = []
        process = mock.Mock()
        process.stdin.close.side_effect = lambda: order.append("close")
        process.terminate.side_effect = lambda: order.append("terminate")
        process.poll.return_value = None
        process.wait.return_value = 0
        session, _ = self._acquire(process)
        session.close()
        self.assertEqual(order, ["close", "terminate"])


class LinuxIdleObserverTests(unittest.TestCase):
    """Idle sources must decline rather than invent a reading."""

    def _logind(self, output):
        from claude_watchdog.power._adapters import logind as logind_module

        with (
            mock.patch.dict(os.environ, {"XDG_SESSION_ID": "3"}),
            mock.patch.object(logind_module.shutil, "which", return_value="/usr/bin/loginctl"),
            mock.patch.object(logind_module.subprocess, "check_output", return_value=output),
        ):
            return logind_module.LogindIdle().observe(300.0)

    def test_idle_hint_of_no_is_not_read_as_user_present(self):
        """wlroots desktops leave IdleHint=no forever; WAITING would hold for ever."""
        observation = self._logind("IdleHint=no\nIdleSinceHintMonotonic=0\n")
        self.assertEqual(observation.kind, IdleKind.UNKNOWN)

    def test_idle_hint_of_yes_still_measures_elapsed_time(self):
        import time as time_module

        since = int((time_module.clock_gettime(time_module.CLOCK_MONOTONIC) - 42) * 1_000_000)
        observation = self._logind(f"IdleHint=yes\nIdleSinceHintMonotonic={since}\n")
        self.assertEqual(observation.kind, IdleKind.WAITING)
        self.assertAlmostEqual(observation.seconds, 42, delta=5)

    def test_gdbus_scalar_parsing(self):
        from claude_watchdog.power._adapters import desktop as desktop_module

        for output, expected in {"(uint64 90210,)\n": 90210, "(uint32 12,)": 12, "(0,)": 0}.items():
            self.assertEqual(desktop_module._gdbus_unsigned(output), expected)
        for output in (None, "", "()", "(-5,)", "nope", "(uint64 x,)"):
            self.assertIsNone(desktop_module._gdbus_unsigned(output))

    def test_desktop_adapters_convert_their_own_units(self):
        from claude_watchdog.power._adapters import desktop as desktop_module

        cases = (
            (desktop_module.MutterIdle(), "(uint64 2500,)", 2.5),
            (desktop_module.ScreenSaverIdle(), "(uint32 61,)", 61.0),
            (desktop_module.XPrintIdle(), "4200\n", 4.2),
        )
        for adapter, output, expected in cases:
            with self.subTest(adapter=adapter.name):
                with mock.patch.object(desktop_module, "_probe", return_value=output):
                    self.assertEqual(adapter.observe(1.0).seconds, expected)
                with mock.patch.object(desktop_module, "_probe", return_value=None):
                    self.assertEqual(adapter.observe(1.0).kind, IdleKind.UNKNOWN)

    def test_chain_takes_the_first_answer(self):
        from claude_watchdog.power import _selection as selection_module

        class Stub:
            def __init__(self, name, observation):
                self.name = name
                self._observation = observation

            def observe(self, threshold_seconds):
                return self._observation

        unknown = wd_power.IdleObservation(kind=IdleKind.UNKNOWN, source="x")
        ready = wd_power.IdleObservation(kind=IdleKind.READY, seconds=9.0, source="y")
        chain = selection_module.IdleChain(
            (Stub("a", unknown), Stub("b", ready), Stub("c", ready))
        )
        self.assertEqual(chain.observe(1.0).source, "y")

    def test_chain_reports_unknown_and_names_what_it_tried(self):
        from claude_watchdog.power import _selection as selection_module

        class Silent:
            def __init__(self, name):
                self.name = name

            def observe(self, threshold_seconds):
                return wd_power.IdleObservation(kind=IdleKind.UNKNOWN, source=self.name)

        chain = selection_module.IdleChain((Silent("alpha"), Silent("beta")))
        observation = chain.observe(1.0)
        self.assertEqual(observation.kind, IdleKind.UNKNOWN)
        self.assertIn("alpha", observation.source)
        self.assertIn("beta", observation.source)

    def test_unavailable_idle_names_the_escape_hatch(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        bundle_idle = mock.Mock()
        bundle_idle.observe.return_value = wd_power.IdleObservation(
            kind=IdleKind.UNKNOWN, source="tried everything"
        )
        with mock.patch.object(wd_power.subprocess, "Popen", return_value=process):
            session = wd_power.open_session(
                PowerPolicy(user_idle_seconds=300), platform="darwin"
            )
        session._bundle = replace(session._bundle, idle=bundle_idle)
        with self.assertRaisesRegex(
            wd_models.PresenceCheckError, "--user-idle-minutes 0"
        ):
            session.poll(check_user_idle=True)
        session.close()


if __name__ == "__main__":
    unittest.main()
