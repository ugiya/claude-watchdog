#!/usr/bin/env python3
"""Install or uninstall claude-watchdog under a user-owned prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_NAME = "claude-watchdog"
MANIFEST_SCHEMA = 1


class InstallError(RuntimeError):
    """The requested operation cannot be performed without overwriting data."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paths(prefix: Path) -> tuple[Path, Path]:
    prefix = prefix.expanduser().resolve()
    destination = prefix / "bin" / RUNTIME_NAME
    manifest = prefix / "share" / RUNTIME_NAME / "install-manifest.json"
    for parent in (destination.parent, manifest.parent):
        existing = parent
        while not existing.exists() and existing != prefix:
            existing = existing.parent
        try:
            existing.resolve().relative_to(prefix)
        except ValueError as error:
            raise InstallError(f"installation path escapes prefix through {existing}") from error
    return destination, manifest


def _load_manifest(path: Path, destination: Path) -> dict[str, object] | None:
    if path.is_symlink():
        raise InstallError(f"refusing symlink installation manifest: {path}")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"cannot read installation manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise InstallError(f"invalid installation manifest: {path}")
    if value.get("schema") != MANIFEST_SCHEMA:
        raise InstallError(f"unsupported installation manifest: {path}")
    if value.get("path") != str(destination):
        raise InstallError(f"installation manifest does not own {destination}")
    digest = value.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise InstallError(f"invalid installation manifest hash: {path}")
    return value


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _assert_replaceable(
    destination: Path,
    manifest_path: Path,
    *,
    force: bool,
) -> dict[str, object] | None:
    try:
        manifest = _load_manifest(manifest_path, destination)
    except InstallError:
        if force:
            return None
        raise
    destination_present = destination.exists() or destination.is_symlink()
    if not destination_present and manifest is None:
        return None
    if destination_present and (not destination.is_file() or destination.is_symlink()):
        raise InstallError(f"refusing to replace non-regular installation {destination}")
    if force:
        return manifest
    if manifest is None:
        raise InstallError(
            f"refusing to replace unrelated file {destination}; use --force to overwrite it"
        )
    if _sha256_file(destination) != manifest["sha256"]:
        raise InstallError(
            f"refusing to replace modified installation {destination}; use --force to overwrite it"
        )
    return manifest


def install(prefix: Path, *, force: bool = False, project_root: Path = PROJECT_ROOT) -> Path:
    destination, manifest_path = _paths(prefix)
    source = project_root / RUNTIME_NAME
    version_path = project_root / "VERSION"
    try:
        runtime = source.read_bytes()
        version = version_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise InstallError(f"cannot read release files: {error}") from error
    if not runtime or not version:
        raise InstallError("release runtime or VERSION is empty")
    _assert_replaceable(destination, manifest_path, force=force)
    digest = _sha256_bytes(runtime)
    manifest = {
        "name": RUNTIME_NAME,
        "path": str(destination),
        "schema": MANIFEST_SCHEMA,
        "sha256": digest,
        "version": version,
    }
    _atomic_write(destination, runtime, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
                  stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _atomic_write(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
    )
    return destination


def uninstall(prefix: Path, *, force: bool = False) -> Path:
    destination, manifest_path = _paths(prefix)
    try:
        manifest = _load_manifest(manifest_path, destination)
    except InstallError:
        if not force:
            raise
        manifest = None
    if manifest is None:
        destination_present = destination.exists() or destination.is_symlink()
        if destination_present:
            if not force:
                raise InstallError(
                    f"refusing to uninstall unrelated file {destination}; use --force to remove it"
                )
            if not destination.is_file() or destination.is_symlink():
                raise InstallError(f"refusing to remove non-regular installation {destination}")
            destination.unlink()
        if force:
            manifest_path.unlink(missing_ok=True)
            for directory in (manifest_path.parent, manifest_path.parent.parent, destination.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return destination
    if destination.exists():
        if not destination.is_file() or destination.is_symlink():
            raise InstallError(f"refusing to remove non-regular installation {destination}")
        if not force and _sha256_file(destination) != manifest["sha256"]:
            raise InstallError(
                f"refusing to remove modified installation {destination}; use --force to remove it"
            )
        destination.unlink()
    manifest_path.unlink(missing_ok=True)
    for directory in (manifest_path.parent, manifest_path.parent.parent, destination.parent):
        try:
            directory.rmdir()
        except OSError:
            pass
    return destination


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        type=Path,
        default=Path.home() / ".local",
        help="installation prefix (default: ~/.local)",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove an owned installation")
    parser.add_argument("--force", action="store_true", help="replace or remove a conflicting file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.uninstall:
            path = uninstall(args.prefix, force=args.force)
            print(f"uninstalled {path}")
        else:
            path = install(args.prefix, force=args.force)
            print(f"installed {path}")
    except InstallError as error:
        print(f"install error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
