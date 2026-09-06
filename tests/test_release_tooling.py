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


class InstallerTests(unittest.TestCase):
    def make_release(self, root: Path, content: bytes = b"exit 0\n") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "claude-watchdog").write_bytes(
            b"#!/bin/sh\nVERSION = '0.1.0'\n" + content
        )
        (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        return root

    def test_install_upgrade_and_uninstall_owned_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = self.make_release(root / "release")
            prefix = root / "prefix"
            destination = installer.install(prefix, project_root=project)
            self.assertEqual(
                destination.read_bytes(), b"#!/bin/sh\nVERSION = '0.1.0'\nexit 0\n"
            )
            self.assertTrue(os.access(destination, os.X_OK))
            manifest_path = prefix / "share" / "claude-watchdog" / "install-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())

            self.make_release(project, b"exit 2\n")
            installer.install(prefix, project_root=project)
            self.assertEqual(
                destination.read_bytes(), b"#!/bin/sh\nVERSION = '0.1.0'\nexit 2\n"
            )
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


class ReleaseBuilderTests(unittest.TestCase):
    def make_project(self, root: Path) -> None:
        (root / "scripts").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "docs").mkdir()
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / ".omx").mkdir()
        (root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        (root / "claude-watchdog").write_text(
            "#!/usr/bin/env python3\nVERSION = '0.1.0'\n", encoding="utf-8"
        )
        (root / "README.md").write_text("public\n", encoding="utf-8")
        (root / "tests" / "test_release_tooling.py").write_text("pass\n", encoding="utf-8")
        (root / "scripts" / "install.py").write_text("pass\n", encoding="utf-8")
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
            self.assertIn(prefix + "tests/test_release_tooling.py", names)
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
            (root / "claude-watchdog").write_text(
                "VERSION = '0.2.0'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(release.ReleaseError, "does not match"):
                release.build_release(root, root / "out")


if __name__ == "__main__":
    unittest.main()
