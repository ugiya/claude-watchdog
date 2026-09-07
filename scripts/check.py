#!/usr/bin/env python3
"""Run the portable claude-watchdog verification suite."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(arguments: list[str], *, environment: dict[str, str] | None = None) -> None:
    print("+", " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=PROJECT_ROOT, env=environment, check=True)


def _python_files() -> list[str]:
    paths = [PROJECT_ROOT / "claude-watchdog"]
    paths.extend(sorted((PROJECT_ROOT / "claude_watchdog").rglob("*.py")))
    paths.extend(sorted((PROJECT_ROOT / "tests").rglob("*.py")))
    paths.extend(sorted((PROJECT_ROOT / "scripts").glob("*.py")))
    return [str(path.relative_to(PROJECT_ROOT)) for path in paths]


def _run_integration(target: Path, environment: dict[str, str]) -> None:
    isolated = environment.copy()
    isolated["WATCHDOG_TEST_TARGET"] = str(target)
    for runner in ("test_watchdog_isolated.py", "test_watchdog_dashboard_pty.py"):
        _run([sys.executable, f"tests/integration/{runner}"], environment=isolated)


def run_checks(*, integration: bool = False) -> None:
    if integration and platform.system() != "Darwin":
        raise RuntimeError("--integration requires macOS")
    python = sys.executable
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("WATCHDOG_TEST_TARGET", None)
    _run([python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], environment=environment)
    with tempfile.TemporaryDirectory(prefix="claude-watchdog-pycache-") as cache:
        compile_environment = environment.copy()
        compile_environment["PYTHONPYCACHEPREFIX"] = cache
        _run([python, "-m", "py_compile", *_python_files()], environment=compile_environment)
    _run([python, "-m", "tabnanny", *_python_files()], environment=environment)
    _run([python, "claude-watchdog", "--help"], environment=environment)
    _run([python, "scripts/install.py", "--help"], environment=environment)
    _run([python, "scripts/build_release.py", "--help"], environment=environment)
    if integration:
        _run_integration(PROJECT_ROOT / "claude-watchdog", environment)
    with tempfile.TemporaryDirectory(prefix="claude-watchdog-install-") as prefix:
        _run([python, "scripts/install.py", "--prefix", prefix], environment=environment)
        installed = str(Path(prefix) / "bin" / "claude-watchdog")
        _run([python, installed, "--help"], environment=environment)
        _run([python, installed, "--version"], environment=environment)
        if integration:
            _run_integration(Path(installed), environment)
        _run([python, "scripts/install.py", "--prefix", prefix, "--uninstall"], environment=environment)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--integration", action="store_true", help="run macOS lifecycle and PTY checks")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_checks(integration=args.integration)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"check failed: {error}", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
