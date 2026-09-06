"""Repository discovery and source-archive layout contracts."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryLayoutTests(unittest.TestCase):
    def test_tests_have_one_home_and_are_checked_and_packaged(self):
        self.assertEqual(list(ROOT.glob("test_*.py")), [])
        expected = {
            "tests/test_claude_watchdog.py", "tests/test_public_release.py",
            "tests/test_release_tooling.py", "tests/test_watchdog_dashboard.py",
            "tests/test_watchdog_demo.py", "tests/test_watchdog_external_lineage.py",
            "tests/test_watchdog_tree.py", "tests/test_repository_layout.py",
            "tests/test_module_boundaries.py", "tests/test_runtime_bundle.py",
            "tests/integration/test_watchdog_isolated.py",
            "tests/integration/test_watchdog_dashboard_pty.py",
        }
        self.assertTrue(all((ROOT / name).is_file() for name in expected))
        checked = set(load_script("check")._python_files())
        packaged = {str(p.relative_to(ROOT)) for p in load_script("build_release").release_paths(ROOT)}
        self.assertLessEqual(expected, checked)
        self.assertLessEqual(expected, packaged)
        self.assertFalse(list((ROOT / "scripts").glob("test_*.py")))

    def test_checker_runs_both_source_and_installed_integration_targets(self):
        from unittest import mock
        checker = load_script("check")
        calls = []
        with mock.patch.object(checker.platform, "system", return_value="Darwin"), \
             mock.patch.object(checker, "_run", side_effect=lambda args, **kw: calls.append((args, kw["environment"]))):
            checker.run_checks(integration=True)
        runners = [(args, env) for args, env in calls if len(args) > 1 and args[1].startswith("tests/integration/")]
        self.assertEqual(len(runners), 4)
        targets = [env["WATCHDOG_TEST_TARGET"] for _, env in runners]
        self.assertEqual(targets.count(str(ROOT / "claude-watchdog")), 2)
        installed = [target for target in targets if target != str(ROOT / "claude-watchdog")]
        self.assertEqual(len(set(installed)), 1)
        self.assertTrue(installed[0].endswith("/bin/claude-watchdog"))

    def test_portable_checker_never_invokes_integration_runners(self):
        from unittest import mock
        checker = load_script("check")
        with mock.patch.object(checker, "_run") as run:
            checker.run_checks()
        self.assertFalse(any(len(call.args[0]) > 1 and call.args[0][1].startswith("tests/integration/") for call in run.call_args_list))
