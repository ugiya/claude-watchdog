#!/usr/bin/env python3
"""Regression tests for the deterministic installed runtime bundle."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle = _load("watchdog_runtime_bundle", ROOT / "scripts" / "runtime_bundle.py")


def make_runtime(root: Path, *, version: str = "0.1.0") -> Path:
    package = root / "claude_watchdog"
    package.mkdir(parents=True)
    sources = {
        "__init__.py": f'VERSION = "{version}"\n',
        "__main__.py": (
            "from .app import main\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n"
        ),
        "app.py": (
            "import argparse\n"
            "from . import VERSION\n"
            "def main():\n"
            "    parser = argparse.ArgumentParser(prog='claude-watchdog')\n"
            "    parser.add_argument('--version', action='version', version=VERSION)\n"
            "    parser.parse_args()\n"
            "    return 0\n"
        ),
    }
    for relative in bundle.REQUIRED_RUNTIME_FILES:
        name = Path(relative).name
        (root / relative).write_text(sources.get(name, f'# {name}\n'), encoding="utf-8")
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return root


class RuntimeBundleTests(unittest.TestCase):
    def test_bundle_is_deterministic_and_contains_only_required_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_runtime(Path(temporary))
            private = root / "claude_watchdog" / "private_config.py"
            private.write_text("TOKEN = 'not public'\n", encoding="utf-8")
            first = bundle.build_runtime_bundle(root)
            os.utime(root / "claude_watchdog" / "app.py", (2_000_000_000, 2_000_000_000))
            second = bundle.build_runtime_bundle(root)

            self.assertEqual(first, second)
            self.assertTrue(first.startswith(b"#!/usr/bin/env python3\n"))
            with zipfile.ZipFile(BytesIO(first)) as archive:
                self.assertEqual(
                    archive.namelist(), ["__main__.py", *bundle.REQUIRED_RUNTIME_FILES]
                )
                self.assertEqual(archive.read("__main__.py"), bundle.ZIPAPP_MAIN)
                self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))
                self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))
            self.assertNotIn(b"private_config", first)
            self.assertNotIn(b"not public", first)

    def test_bundle_rejects_missing_and_symlinked_required_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_runtime(Path(temporary))
            required = root / "claude_watchdog" / "power.py"
            required.unlink()
            with self.assertRaisesRegex(bundle.BundleError, "missing required runtime file"):
                bundle.build_runtime_bundle(root)

            required.write_text("# outside\n", encoding="utf-8")
            target = root / "outside.py"
            target.write_text("# outside\n", encoding="utf-8")
            required.unlink()
            required.symlink_to(target)
            with self.assertRaisesRegex(bundle.BundleError, "symlink"):
                bundle.build_runtime_bundle(root)

    def test_bundle_rejects_symlinked_required_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real_root = make_runtime(base / "real")
            linked_root = base / "linked"
            linked_root.mkdir()
            (linked_root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (linked_root / "claude_watchdog").symlink_to(real_root / "claude_watchdog", target_is_directory=True)
            with self.assertRaisesRegex(bundle.BundleError, "parent.*symlink|symlink.*parent"):
                bundle.build_runtime_bundle(linked_root)

    def test_bundle_rejects_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = make_runtime(Path(temporary))
            (root / "VERSION").write_text("0.2.0\n", encoding="utf-8")
            with self.assertRaisesRegex(bundle.BundleError, "does not match"):
                bundle.build_runtime_bundle(root)

    @unittest.skipUnless(os.name == "posix", "direct shebang execution requires POSIX")
    def test_detached_bundle_runs_help_and_version_without_source_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = make_runtime(base / "source")
            installer = _load("watchdog_detached_installer", ROOT / "scripts" / "install.py")
            command = installer.install(base / "prefix", project_root=source)
            env = {"HOME": str(base / "home"), "PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""}
            source.rename(base / "source-unavailable")

            help_result = subprocess.run(
                [str(command), "--help"], cwd=base, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            version_result = subprocess.run(
                [str(command), "--version"], cwd=base, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: claude-watchdog", help_result.stdout)
            self.assertEqual(version_result.returncode, 0, version_result.stderr)
            self.assertEqual(version_result.stdout.strip(), "0.1.0")

    @unittest.skipUnless(os.name == "posix", "direct shebang execution requires POSIX")
    def test_real_source_package_and_bundle_cli_outputs_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            command = base / "claude-watchdog"
            command.write_bytes(bundle.build_runtime_bundle(ROOT))
            command.chmod(0o755)
            env = {
                "HOME": str(base / "home"),
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": "",
            }
            entrypoints = (
                ([sys.executable, str(ROOT / "claude-watchdog")], ROOT),
                ([sys.executable, "-m", "claude_watchdog"], ROOT),
                ([str(command)], base),
            )
            cases = (
                (("--help",), 0),
                (("--version",), 0),
                (("--source", "invalid"), 2),
            )
            for arguments, expected_code in cases:
                with self.subTest(arguments=arguments):
                    results = [
                        subprocess.run(
                            [*entrypoint, *arguments], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                        )
                        for entrypoint, cwd in entrypoints
                    ]
                    self.assertEqual(
                        [result.returncode for result in results], [expected_code] * 3
                    )
                    self.assertEqual(results[0].stdout, results[1].stdout)
                    self.assertEqual(results[1].stdout, results[2].stdout)
                    self.assertEqual(results[0].stderr, results[1].stderr)
                    self.assertEqual(results[1].stderr, results[2].stderr)


if __name__ == "__main__":
    unittest.main()
