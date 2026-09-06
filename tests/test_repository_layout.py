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
            "tests/integration/test_watchdog_isolated.py",
            "tests/integration/test_watchdog_dashboard_pty.py",
        }
        self.assertTrue(all((ROOT / name).is_file() for name in expected))
        checked = set(load_script("check")._python_files())
        packaged = {str(p.relative_to(ROOT)) for p in load_script("build_release").release_paths(ROOT)}
        self.assertLessEqual(expected, checked)
        self.assertLessEqual(expected, packaged)
        self.assertFalse(list((ROOT / "scripts").glob("test_*.py")))
