#!/usr/bin/env python3
"""Regression tests for installation and deterministic release tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load("watchdog_installer", ROOT / "scripts" / "install.py")
release = _load("watchdog_release", ROOT / "scripts" / "build_release.py")

RUNTIME_FILES = (
    "__init__.py", "__main__.py", "models.py", "text.py", "presentation.py",
    "config.py", "activity.py", "metadata.py", "dashboard.py", "reporting.py",
    "power.py", "app.py",
)


class InstallerTests(unittest.TestCase):
    def make_release(self, root: Path, content: bytes = b"exit 0\n") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        package = root / "claude_watchdog"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text('VERSION = "0.1.0"\n', encoding="utf-8")
        (package / "__main__.py").write_text(
            "from .app import main\nraise SystemExit(main())\n", encoding="utf-8"
        )
        (package / "app.py").write_bytes(
            b"def main():\n    " + content.replace(b"exit ", b"return ")
        )
        for name in RUNTIME_FILES:
            path = package / name
            if not path.exists():
                path.write_text(f"# {name}\n", encoding="utf-8")
        (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        return root

    def test_install_upgrade_and_uninstall_owned_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_release(root / "release")
            prefix = root / "prefix"
            destination = installer.install(prefix, project_root=project)
            first_bytes = destination.read_bytes()
            self.assertTrue(first_bytes.startswith(b"#!/usr/bin/env python3\n"))
            self.assertTrue(os.access(destination, os.X_OK))
            manifest_path = prefix / "share" / "claude-watchdog" / "install-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())

            self.make_release(project, b"exit 2\n")
            installer.install(prefix, project_root=project)
            self.assertNotEqual(destination.read_bytes(), first_bytes)
            installer.uninstall(prefix)
            self.assertFalse(destination.exists())
            self.assertFalse(manifest_path.exists())

    def test_install_refuses_unrelated_or_modified_runtime_without_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_release(root / "release")
            prefix = root / "prefix"
            destination = prefix / "bin" / "claude-watchdog"
            destination.parent.mkdir(parents=True)
            destination.write_text("unrelated\n", encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "unrelated"):
                installer.install(prefix, project_root=project)
            self.assertEqual(destination.read_text(encoding="utf-8"), "unrelated\n")

            installer.install(prefix, force=True, project_root=project)
            destination.write_text("locally modified\n", encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "modified"):
                installer.install(prefix, project_root=project)
            self.assertEqual(destination.read_text(encoding="utf-8"), "locally modified\n")

    def test_uninstall_preserves_modified_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_release(root / "release")
            prefix = root / "prefix"
            destination = installer.install(prefix, project_root=project)
            destination.write_text("local edit\n", encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "modified"):
                installer.uninstall(prefix)
            self.assertEqual(destination.read_text(encoding="utf-8"), "local edit\n")
            installer.uninstall(prefix, force=True)
            self.assertFalse(destination.exists())

            unrelated = root / "other" / "bin" / "claude-watchdog"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("keep me\n", encoding="utf-8")
            with self.assertRaisesRegex(installer.InstallError, "unrelated"):
                installer.uninstall(root / "other")
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me\n")
            installer.uninstall(root / "other", force=True)
            self.assertFalse(unrelated.exists())

    def test_install_refuses_prefix_directory_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_release(root / "release")
            prefix = root / "prefix"
            outside = root / "outside"
            outside.mkdir()
            prefix.mkdir()
            (prefix / "bin").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(installer.InstallError, "escapes prefix"):
                installer.install(prefix, project_root=project)
            self.assertFalse((outside / "claude-watchdog").exists())

            safe_prefix = root / "safe-prefix"
            destination = safe_prefix / "bin" / "claude-watchdog"
            destination.parent.mkdir(parents=True)
            destination.symlink_to(root / "missing-target")
            with self.assertRaisesRegex(installer.InstallError, "non-regular"):
                installer.install(safe_prefix, force=True, project_root=project)
            self.assertTrue(destination.is_symlink())

    def test_install_upgrades_legacy_single_file_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_release(root / "release")
            prefix = root / "prefix"
            destination, manifest_path = installer._paths(prefix)
            destination.parent.mkdir(parents=True)
            manifest_path.parent.mkdir(parents=True)
            legacy = b"#!/usr/bin/env python3\nVERSION = '0.1.0'\n"
            destination.write_bytes(legacy)
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "claude-watchdog",
                        "path": str(destination),
                        "schema": 1,
                        "sha256": hashlib.sha256(legacy).hexdigest(),
                        "version": "0.1.0",
                    }
                ),
                encoding="utf-8",
            )

            installer.install(prefix, project_root=project)

            self.assertTrue(destination.read_bytes().startswith(b"#!/usr/bin/env python3\nPK"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest()
            )

    def test_bundle_failure_preserves_existing_install_and_manifest(self):
        for failure in ("missing file", "symlink file", "symlink parent"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = self.make_release(root / "release")
                prefix = root / "prefix"
                destination = installer.install(prefix, project_root=project)
                manifest_path = prefix / "share" / "claude-watchdog" / "install-manifest.json"
                installed_before = destination.read_bytes()
                manifest_before = manifest_path.read_bytes()
                power = project / "claude_watchdog" / "power.py"
                if failure == "missing file":
                    power.unlink()
                elif failure == "symlink file":
                    power.unlink()
                    outside = project / "outside.py"
                    outside.write_text("# outside\n", encoding="utf-8")
                    power.symlink_to(outside)
                else:
                    real_package = project / "real-package"
                    (project / "claude_watchdog").rename(real_package)
                    (project / "claude_watchdog").symlink_to(
                        real_package, target_is_directory=True
                    )

                with self.assertRaises(installer.InstallError):
                    installer.install(prefix, project_root=project)

                self.assertEqual(destination.read_bytes(), installed_before)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)


class ReleaseBuilderTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        (root / "scripts").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "docs").mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / ".omx").mkdir()
        (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (root / "claude-watchdog").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        package = root / "claude_watchdog"
        package.mkdir()
        for name in RUNTIME_FILES:
            value = 'VERSION = "0.1.0"\n' if name == "__init__.py" else f"# {name}\n"
            (package / name).write_text(value, encoding="utf-8")
        (root / "README.md").write_text("public\n", encoding="utf-8")
        (root / "tests" / "test_release_tooling.py").write_text("pass\n", encoding="utf-8")
        (root / "scripts" / "install.py").write_text("pass\n", encoding="utf-8")
        (root / "scripts" / "build_release.py").write_text("pass\n", encoding="utf-8")
        (root / "scripts" / "runtime_bundle.py").write_text("pass\n", encoding="utf-8")
        (root / "scripts" / "check.py").write_text("pass\n", encoding="utf-8")
        (root / "docs" / "usage.md").write_text("public docs\n", encoding="utf-8")
        (root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        (root / ".git" / "config").write_text("private git state\n", encoding="utf-8")
        (root / ".omx" / "session.json").write_text("private context\n", encoding="utf-8")
        (root / "private-notes.md").write_text("private context\n", encoding="utf-8")
        (root / "test_private_notes.py").write_text("private context\n", encoding="utf-8")

    def test_release_is_deterministic_and_contains_only_public_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            self.make_project(root)
            first, sums = release.build_release(root, root / "out-one")
            first_bytes = first.read_bytes()
            os.utime(root / "README.md", (2_000_000_000, 2_000_000_000))
            second, _ = release.build_release(root, root / "out-two")
            self.assertEqual(first_bytes, second.read_bytes())
            expected_digest = hashlib.sha256(first_bytes).hexdigest()
            self.assertEqual(sums.read_text(encoding="ascii"), f"{expected_digest}  {first.name}\n")

            with tarfile.open(fileobj=io.BytesIO(first_bytes), mode="r:gz") as archive:
                names = archive.getnames()
            prefix = "claude-watchdog-0.1.0/"
            self.assertIn(prefix + "README.md", names)
            self.assertIn(prefix + "docs/usage.md", names)
            self.assertIn(prefix + ".github/workflows/ci.yml", names)
            self.assertIn(prefix + "scripts/check.py", names)
            self.assertIn(prefix + "scripts/runtime_bundle.py", names)
            self.assertIn(prefix + "tests/test_release_tooling.py", names)
            self.assertIn(prefix + "claude_watchdog/app.py", names)
            self.assertNotIn(prefix + ".git/config", names)
            self.assertNotIn(prefix + ".omx/session.json", names)
            self.assertNotIn(prefix + "private-notes.md", names)
            self.assertNotIn(prefix + "test_private_notes.py", names)
            self.assertTrue(all(".." not in Path(name).parts for name in names))

    def test_release_rejects_invalid_version_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            (root / "VERSION").write_text("../../private\n", encoding="utf-8")
            with self.assertRaisesRegex(release.ReleaseError, "invalid release version"):
                release.build_release(root, root / "out")
            (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (root / "docs" / "link.md").symlink_to(root / "private-notes.md")
            with self.assertRaisesRegex(release.ReleaseError, "regular files"):
                release.build_release(root, root / "out")

    def test_release_rejects_runtime_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            (root / "claude_watchdog" / "__init__.py").write_text(
                "VERSION = '0.2.0'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(release.ReleaseError, "does not match"):
                release.build_release(root, root / "out")

    def test_release_rejects_incomplete_or_symlinked_runtime_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_project(root)
            power = root / "claude_watchdog" / "power.py"
            power.unlink()
            with self.assertRaisesRegex(release.ReleaseError, "missing required runtime file"):
                release.build_release(root, root / "out")

            power.write_text("# power.py\n", encoding="utf-8")
            real_package = root / "real-package"
            (root / "claude_watchdog").rename(real_package)
            (root / "claude_watchdog").symlink_to(real_package, target_is_directory=True)
            with self.assertRaisesRegex(release.ReleaseError, "parent is a symlink"):
                release.build_release(root, root / "out")


if __name__ == "__main__":
    unittest.main()
