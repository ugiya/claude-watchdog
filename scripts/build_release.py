#!/usr/bin/env python3
"""Build a deterministic claude-watchdog source release and checksum."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from runtime_bundle import (
    BundleError,
    REQUIRED_RUNTIME_FILES,
    required_runtime_paths,
    runtime_version,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = (
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "LICENSE.md",
    "README.md",
    "SECURITY.md",
    "VERSION",
    "claude-watchdog",
)
SCRIPT_FILES = (
    "scripts/benchmark_watchdog.py",
    "scripts/build_release.py",
    "scripts/check.py",
    "scripts/check_public.py",
    "scripts/demo.py",
    "scripts/install.py",
    "scripts/runtime_bundle.py",
)
TEST_FILES = (
    "tests/integration/test_watchdog_dashboard_pty.py",
    "tests/integration/test_watchdog_isolated.py",
    "tests/test_repository_layout.py",
    "tests/test_claude_watchdog.py",
    "tests/test_public_release.py",
    "tests/test_release_tooling.py",
    "tests/test_runtime_bundle.py",
    "tests/test_module_boundaries.py",
    "tests/test_watchdog_dashboard.py",
    "tests/test_watchdog_demo.py",
    "tests/test_watchdog_external_lineage.py",
    "tests/test_watchdog_tree.py",
)
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")


class ReleaseError(RuntimeError):
    """The source tree cannot be packaged safely."""


def _version(project_root: Path) -> str:
    try:
        version = (project_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ReleaseError(f"cannot read VERSION: {error}") from error
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseError(f"invalid release version: {version!r}")
    return version


def release_paths(project_root: Path) -> list[Path]:
    try:
        required_runtime_paths(project_root)
    except BundleError as error:
        raise ReleaseError(str(error)) from error
    candidates = [project_root / name for name in ROOT_FILES]
    candidates.extend(project_root / name for name in REQUIRED_RUNTIME_FILES)
    candidates.extend(project_root / name for name in TEST_FILES)
    candidates.extend(project_root / name for name in SCRIPT_FILES)
    for directory_name in ("docs", ".github"):
        directory = project_root / directory_name
        if directory.exists():
            candidates.extend(path for path in directory.rglob("*") if not path.is_dir())

    selected: dict[str, Path] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            relative = path.relative_to(project_root)
        except ValueError as error:
            raise ReleaseError(f"release path escapes project root: {path}") from error
        posix = PurePosixPath(relative.as_posix())
        if posix.is_absolute() or ".." in posix.parts:
            raise ReleaseError(f"unsafe release path: {relative}")
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"release entries must be regular files: {relative}")
        selected[posix.as_posix()] = path
    required = (
        "VERSION",
        "claude-watchdog",
        "scripts/install.py",
        "scripts/runtime_bundle.py",
        *REQUIRED_RUNTIME_FILES,
    )
    missing = [name for name in required if name not in selected]
    if missing:
        raise ReleaseError(f"missing required release files: {', '.join(missing)}")
    return [selected[name] for name in sorted(selected)]


def _runtime_version(project_root: Path) -> str:
    try:
        return runtime_version(project_root)
    except BundleError as error:
        raise ReleaseError(str(error)) from error


def _archive_bytes(project_root: Path, version: str) -> bytes:
    raw_tar = io.BytesIO()
    prefix = f"claude-watchdog-{version}"
    with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path in release_paths(project_root):
            relative = path.relative_to(project_root).as_posix()
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{prefix}/{relative}")
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            executable = relative == "claude-watchdog" or relative.startswith("scripts/")
            info.mode = 0o755 if executable else 0o644
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, mtime=0, compresslevel=9) as output:
        output.write(raw_tar.getvalue())
    return compressed.getvalue()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_release(project_root: Path = PROJECT_ROOT, output_dir: Path | None = None) -> tuple[Path, Path]:
    project_root = project_root.resolve()
    version = _version(project_root)
    runtime_version = _runtime_version(project_root)
    if runtime_version != version:
        raise ReleaseError(
            f"VERSION file {version!r} does not match runtime VERSION {runtime_version!r}"
        )
    output_dir = (output_dir or project_root / "dist").resolve()
    archive = output_dir / f"claude-watchdog-{version}.tar.gz"
    checksum = output_dir / "SHA256SUMS"
    data = _archive_bytes(project_root, version)
    digest = hashlib.sha256(data).hexdigest()
    _atomic_write(archive, data)
    _atomic_write(checksum, f"{digest}  {archive.name}\n".encode("ascii"))
    archive.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    checksum.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return archive, checksum


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="artifact directory (default: ./dist)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        archive, checksum = build_release(output_dir=args.output_dir)
    except (OSError, ReleaseError) as error:
        print(f"release error: {error}", file=sys.stderr)
        return 1
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
