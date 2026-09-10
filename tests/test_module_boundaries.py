"""Structural regression tests for the modular runtime package."""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "claude_watchdog"
MODULES = {
    "__init__",
    "__main__",
    "activity",
    "app",
    "config",
    "dashboard",
    "metadata",
    "models",
    "power",
    "presentation",
    "process_lineage",
    "reporting",
    "text",
}


def _package_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                imports.add(node.module.split(".", 1)[0])
            else:
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == "claude_watchdog":
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif node.module and node.module.startswith("claude_watchdog."):
                imports.add(node.module.split(".", 2)[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("claude_watchdog."):
                    imports.add(alias.name.split(".", 2)[1])
    return imports


class ModuleBoundaryTests(unittest.TestCase):
    def test_import_parser_covers_relative_and_absolute_package_forms(self) -> None:
        cases = (
            ("from . import dashboard", {"dashboard"}),
            ("from .metadata import load_session_metadata", {"metadata"}),
            ("import claude_watchdog.reporting as reporting", {"reporting"}),
            ("from claude_watchdog import dashboard", {"dashboard"}),
            ("from claude_watchdog.metadata import load_session_metadata", {"metadata"}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "example.py"
            for source, expected in cases:
                with self.subTest(source=source):
                    path.write_text(source + "\n", encoding="utf-8")
                    self.assertEqual(_package_imports(path), expected)

    def test_runtime_package_has_exact_approved_modules(self) -> None:
        self.assertTrue(PACKAGE.is_dir())
        self.assertEqual({path.stem for path in PACKAGE.glob("*.py")}, MODULES)

    def test_entrypoints_are_thin_and_explicit(self) -> None:
        launcher = ROOT / "claude-watchdog"
        self.assertLess(len(launcher.read_text(encoding="utf-8").splitlines()), 30)
        self.assertIn("from claude_watchdog import app", launcher.read_text(encoding="utf-8"))
        main_source = (PACKAGE / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("raise SystemExit(app.main())", main_source)

    def test_init_contains_only_literal_version(self) -> None:
        tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
        executable = tree.body[1:] if ast.get_docstring(tree) is not None else tree.body
        self.assertEqual(len(executable), 1)
        assignment = executable[0]
        self.assertIsInstance(assignment, ast.Assign)
        self.assertEqual([target.id for target in assignment.targets], ["VERSION"])
        self.assertIsInstance(assignment.value, ast.Constant)
        self.assertIsInstance(assignment.value.value, str)

    def test_package_import_graph_is_acyclic_and_respects_boundaries(self) -> None:
        graph = {
            module: _package_imports(PACKAGE / f"{module}.py") & MODULES
            for module in MODULES
        }
        forbidden = {
            "activity": {"dashboard", "metadata", "process_lineage", "reporting"},
            "app": {"process_lineage"},
            "config": {"dashboard", "metadata", "process_lineage", "reporting"},
            "metadata": {"dashboard", "reporting"},
            "power": {"dashboard", "metadata", "process_lineage", "reporting"},
            "reporting": {"dashboard"},
        }
        for module, denied in forbidden.items():
            self.assertFalse(graph[module] & denied, f"{module}: {graph[module] & denied}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(module: str) -> None:
            if module in visiting:
                self.fail(f"package import cycle reaches {module}")
            if module in visited:
                return
            visiting.add(module)
            for dependency in graph[module]:
                visit(dependency)
            visiting.remove(module)
            visited.add(module)

        for module in graph:
            visit(module)

        self.assertEqual(
            {
                module
                for module, dependencies in graph.items()
                if "process_lineage" in dependencies
            },
            {"metadata"},
        )

    def test_palette_is_shared_below_dashboard_and_reporting(self) -> None:
        presentation = (PACKAGE / "presentation.py").read_text(encoding="utf-8")
        self.assertIn("SOURCE_COLORS =", presentation)
        self.assertIn("LABEL_COLORS =", presentation)
        self.assertIn("presentation", _package_imports(PACKAGE / "dashboard.py"))
        self.assertIn("presentation", _package_imports(PACKAGE / "reporting.py"))

    def test_importing_runtime_modules_has_no_operational_side_effects(self) -> None:
        script = """
import logging
import os
import pathlib
import subprocess

def blocked(*args, **kwargs):
    raise AssertionError("operational side effect during import")

subprocess.Popen = blocked
subprocess.run = blocked
pathlib.Path.mkdir = blocked
pathlib.Path.open = blocked
logging.basicConfig = blocked
logging.FileHandler = blocked
os.scandir = blocked

import sqlite3
sqlite3.connect = blocked

import claude_watchdog.activity
import claude_watchdog.app
import claude_watchdog.config
import claude_watchdog.dashboard
import claude_watchdog.metadata
import claude_watchdog.models
import claude_watchdog.power
import claude_watchdog.presentation
import claude_watchdog.process_lineage
import claude_watchdog.reporting
import claude_watchdog.text
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_maintainer_script_help_works_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for script in ("demo.py", "benchmark_watchdog.py"):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / script), "--help"],
                    cwd=temporary,
                    text=True,
                    capture_output=True,
                    env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_benchmark_target_selects_source_tree_and_zip(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "watchdog_benchmark_boundary", ROOT / "scripts" / "benchmark_watchdog.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        benchmark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(benchmark)

        saved_modules = {name: module for name, module in sys.modules.items()
                         if name == "claude_watchdog" or name.startswith("claude_watchdog.")}
        def restore_modules():
            for name in tuple(sys.modules):
                if name == "claude_watchdog" or name.startswith("claude_watchdog."):
                    del sys.modules[name]
            sys.modules.update(saved_modules)
        self.addCleanup(restore_modules)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            archive_source = root / "archive"
            shutil.copytree(PACKAGE, source / "claude_watchdog")
            shutil.copytree(PACKAGE, archive_source / "claude_watchdog")
            launcher = source / "claude-watchdog"
            launcher.write_text("# source selector\n", encoding="utf-8")
            from scripts.runtime_bundle import build_runtime_bundle
            (archive_source / "VERSION").write_bytes((ROOT / "VERSION").read_bytes())
            # Source release archives deliberately have epoch-zero timestamps.
            for source_file in archive_source.rglob("*.py"):
                os.utime(source_file, (0, 0))
            archive = root / "claude-watchdog"
            archive.write_bytes(build_runtime_bundle(archive_source))

            for target in (launcher, archive):
                modules = benchmark._load_target(target.resolve())
                self.assertEqual([module.__name__ for module in modules], [
                    "claude_watchdog.models",
                    "claude_watchdog.activity",
                    "claude_watchdog.config",
                ])
                import_root = target if target == archive else target.parent
                expected = str(import_root.resolve() / "claude_watchdog") + os.sep
                self.assertTrue(all(module.__file__.startswith(expected) for module in modules))

            # A sibling whose name merely shares the selected root's prefix
            # must not satisfy --target via an ambient sys.path entry.
            chosen = root / "chosen"
            chosen.mkdir()
            sibling = root / "chosen-other"
            shutil.copytree(PACKAGE, sibling / "claude_watchdog")
            sys.path.insert(0, str(sibling.resolve()))
            try:
                with self.assertRaisesRegex(RuntimeError, "target"):
                    benchmark._load_target(chosen.resolve())
            finally:
                sys.path.remove(str(sibling.resolve()))


if __name__ == "__main__":
    unittest.main()
