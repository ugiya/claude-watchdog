#!/usr/bin/env python3
"""Build the deterministic single-file executable claude-watchdog runtime."""

from __future__ import annotations

import ast
import io
import zipfile
from pathlib import Path


SHEBANG = b"#!/usr/bin/env python3\n"
ZIPAPP_MAIN = (
    b"from claude_watchdog.app import main\n"
    b"raise SystemExit(main())\n"
)
PACKAGE_NAME = "claude_watchdog"
REQUIRED_RUNTIME_FILES = (
    f"{PACKAGE_NAME}/__init__.py",
    f"{PACKAGE_NAME}/__main__.py",
    f"{PACKAGE_NAME}/models.py",
    f"{PACKAGE_NAME}/text.py",
    f"{PACKAGE_NAME}/presentation.py",
    f"{PACKAGE_NAME}/config.py",
    f"{PACKAGE_NAME}/activity.py",
    f"{PACKAGE_NAME}/metadata.py",
    f"{PACKAGE_NAME}/dashboard.py",
    f"{PACKAGE_NAME}/reporting.py",
    f"{PACKAGE_NAME}/power.py",
    f"{PACKAGE_NAME}/app.py",
)
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_REGULAR_FILE_MODE = 0o100644


class BundleError(RuntimeError):
    """The source tree cannot produce a safe, complete runtime bundle."""


def _required_regular_file(project_root: Path, relative: str) -> Path:
    root = Path(project_root)
    if root.is_symlink():
        raise BundleError(f"required runtime parent is a symlink: {root}")
    if not root.exists() or not root.is_dir():
        raise BundleError(f"missing required runtime parent: {root}")

    path = root / relative
    current = root
    for part in Path(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise BundleError(f"required runtime parent is a symlink: {current}")
        if not current.exists() or not current.is_dir():
            raise BundleError(f"missing required runtime parent: {current}")
    if path.is_symlink():
        raise BundleError(f"required runtime file is a symlink: {path}")
    if not path.exists() or not path.is_file():
        raise BundleError(f"missing required runtime file: {path}")
    return path


def required_runtime_paths(project_root: Path) -> tuple[Path, ...]:
    """Return validated runtime source paths in canonical archive order."""
    return tuple(
        _required_regular_file(project_root, relative)
        for relative in REQUIRED_RUNTIME_FILES
    )


def _literal_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise BundleError(f"cannot inspect runtime version: {error}") from error
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "VERSION" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
            return value.value
        raise BundleError("runtime VERSION must be a non-empty literal string")
    raise BundleError("runtime package does not declare VERSION")


def runtime_version(project_root: Path) -> str:
    """Read the package's literal VERSION after validating its source path."""
    init_path = _required_regular_file(project_root, REQUIRED_RUNTIME_FILES[0])
    return _literal_version(init_path)


def _release_version(project_root: Path) -> str:
    version_path = _required_regular_file(project_root, "VERSION")
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise BundleError(f"cannot read VERSION: {error}") from error
    if not version:
        raise BundleError("VERSION is empty")
    return version


def build_runtime_bundle(project_root: Path) -> bytes:
    """Build deterministic executable ZIP bytes from the required source allowlist."""
    paths = required_runtime_paths(project_root)
    release_version = _release_version(project_root)
    package_version = _literal_version(paths[0])
    if package_version != release_version:
        raise BundleError(
            f"VERSION file {release_version!r} does not match runtime VERSION {package_version!r}"
        )

    output = io.BytesIO()
    output.write(SHEBANG)
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        main_info = zipfile.ZipInfo("__main__.py", date_time=_FIXED_ZIP_TIME)
        main_info.compress_type = zipfile.ZIP_STORED
        main_info.create_system = 3
        main_info.external_attr = _REGULAR_FILE_MODE << 16
        archive.writestr(main_info, ZIPAPP_MAIN)
        for relative, path in zip(REQUIRED_RUNTIME_FILES, paths):
            try:
                data = path.read_bytes()
            except OSError as error:
                raise BundleError(f"cannot read required runtime file {path}: {error}") from error
            info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = _REGULAR_FILE_MODE << 16
            archive.writestr(info, data)
    return output.getvalue()
