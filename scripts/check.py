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
    paths.extend(sorted(PROJECT_ROOT.glob("test_*.py")))
    paths.extend(sorted((PROJECT_ROOT / "scripts").glob("*.py")))
    return [str(path.relative_to(PROJECT_ROOT)) for path in paths]


def run_checks(*, integration: bool = False) -> None:
    python = sys.executable
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    _run([python, "-m", "unittest", "discover", "-p", "test_*.py"], environment=environment)
    with tempfile.TemporaryDirectory(prefix="claude-watchdog-pycache-") as cache:
        compile_environment = environment.copy()
        compile_environment["PYTHONPYCACHEPREFIX"] = cache
        _run([python, "-m", "py_compile", *_python_files()], environment=compile_environment)
    _run([python, "-m", "tabnanny", *_python_files()], environment=environment)
    _run([python, "claude-watchdog", "--help"], environment=environment)
    _run([python, "scripts/install.py", "--help"], environment=environment)
    _run([python, "scripts/build_release.py", "--help"], environment=environment)
    with tempfile.TemporaryDirectory(prefix="claude-watchdog-install-") as prefix:
        _run([python, "scripts/install.py", "--prefix", prefix], environment=environment)
        installed = str(Path(prefix) / "bin" / "claude-watchdog")
        _run([python, installed, "--help"], environment=environment)
        _run([python, "scripts/install.py", "--prefix", prefix, "--uninstall"], environment=environment)
    if integration:
        if platform.system() != "Darwin":
            raise RuntimeError("--integration requires macOS")
        _run([python, "scripts/test_watchdog_isolated.py"], environment=environment)
        _run([python, "scripts/test_watchdog_dashboard_pty.py"], environment=environment)


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
