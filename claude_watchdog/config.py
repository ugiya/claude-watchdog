"""Profile validation and command-line configuration."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import unicodedata
from pathlib import Path

from . import models as models_module
from . import text as text_module
from . import VERSION

def claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def claude_sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def claude_profiles_path() -> Path:
    return Path.home() / ".config" / "claude-watchdog" / "profiles.json"


def load_claude_profiles(
    path: Path, *, required: bool = False
) -> tuple[models_module.ClaudeProfile, ...]:
    """Load and validate the bounded, launch-frozen Claude profile registry."""
    try:
        path = path.expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise models_module.ActivityReadError(f"unable to expand Claude profiles file {path}: {exc}") from exc
    try:
        with path.open("rb") as handle:
            raw = handle.read(models_module.MAX_PROFILES_BYTES + 1)
    except FileNotFoundError as exc:
        if not required:
            return ()
        raise models_module.ActivityReadError(f"Claude profiles file does not exist: {path}") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise models_module.ActivityReadError(f"unable to read Claude profiles file {path}: {exc}") from exc
    if len(raw) > models_module.MAX_PROFILES_BYTES:
        raise models_module.ActivityReadError("Claude profiles file exceeds the 64 KiB limit")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise models_module.ActivityReadError(f"Claude profiles file is not valid JSON: {path}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "claude_profiles"}:
        raise models_module.ActivityReadError(
            "Claude profiles file must contain only version and claude_profiles"
        )
    if type(document["version"]) is not int or document["version"] != 1:
        raise models_module.ActivityReadError("Claude profiles file version must be 1")
    entries = document["claude_profiles"]
    if not isinstance(entries, list):
        raise models_module.ActivityReadError("claude_profiles must be an array")
    if len(entries) > models_module.MAX_CLAUDE_PROFILES:
        raise models_module.ActivityReadError("claude_profiles may contain at most 32 entries")

    try:
        builtin_root = claude_projects_dir().expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise models_module.ActivityReadError(f"unable to resolve built-in Claude projects directory: {exc}") from exc
    profiles: list[models_module.ClaudeProfile] = []
    ids: set[str] = set()
    roots = {builtin_root}
    for index, entry in enumerate(entries):
        location = f"claude_profiles[{index}]"
        allowed_keys = {"id", "label", "projects_dir"}
        if (
            not isinstance(entry, dict)
            or not {"id", "projects_dir"}.issubset(entry)
            or not set(entry).issubset(allowed_keys)
        ):
            raise models_module.ActivityReadError(
                f"{location} must contain id, projects_dir, and optional label"
            )
        profile_id, label, raw_root = (
            entry["id"], entry.get("label", entry["id"]), entry["projects_dir"]
        )
        if (
            not isinstance(profile_id, str)
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", profile_id) is None
        ):
            raise models_module.ActivityReadError(f"{location}.id must be a lowercase slug of at most 64 characters")
        if profile_id in ids:
            raise models_module.ActivityReadError(f"duplicate Claude profile id: {profile_id}")
        if not isinstance(label, str):
            raise models_module.ActivityReadError(f"{location}.label must be 1-64 printable cells")
        normalized_label = unicodedata.normalize("NFC", label).strip()
        if (
            not normalized_label
            or text_module.text_cells(normalized_label) > 64
            or any(
                unicodedata.category(char).startswith("C")
                or unicodedata.category(char) in {"Zl", "Zp"}
                for char in normalized_label
            )
        ):
            raise models_module.ActivityReadError(f"{location}.label must be 1-64 printable cells")
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise models_module.ActivityReadError(f"{location}.projects_dir must be an absolute path")
        try:
            expanded = Path(raw_root).expanduser()
        except (OSError, RuntimeError, ValueError) as exc:
            raise models_module.ActivityReadError(f"unable to expand {location}.projects_dir: {exc}") from exc
        if not expanded.is_absolute():
            raise models_module.ActivityReadError(f"{location}.projects_dir must be an absolute path")
        try:
            root = expanded.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise models_module.ActivityReadError(f"unable to resolve {location}.projects_dir: {exc}") from exc
        if root == builtin_root:
            raise models_module.ActivityReadError(f"{location}.projects_dir aliases the built-in Claude root")
        if root in roots:
            raise models_module.ActivityReadError(f"duplicate Claude profile projects_dir: {root}")
        ids.add(profile_id)
        roots.add(root)
        profiles.append(models_module.ClaudeProfile(profile_id, normalized_label, root))
    return tuple(profiles)


def codex_sessions_dir() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home / "sessions"


def omx_log_dirs() -> list[Path]:
    """Return the global OMX JSONL directory from the agreed source scope."""
    return [Path.home() / ".omx" / "logs"]


def opencode_database_path() -> Path | None:
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    data_home = (
        Path(xdg_data_home).expanduser()
        if xdg_data_home
        else Path.home() / ".local" / "share"
    )
    data_dir = data_home / "opencode"
    override = os.environ.get("OPENCODE_DB")
    if override == ":memory:":
        return None
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else data_dir / path
    return data_dir / "opencode.db"


def _source_names(source: str) -> tuple[str, ...]:
    normalized = source.strip().lower()
    if normalized == "auto":
        return models_module.ALL_SOURCES
    if normalized == "codex-omx":
        return ("codex", "omx")
    if normalized in models_module.ALL_SOURCES:
        return (normalized,)
    raise ValueError(f"unsupported source: {source!r}")


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a finite number greater than zero")
    return parsed


def _hide_path(value: str) -> Path:
    if not value.strip():
        raise argparse.ArgumentTypeError("hide path must be a non-empty path")
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"unable to resolve hide path {value!r}: {exc}") from exc


def _nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be a finite non-negative number")
    return parsed


def parse_args(argv: list[str] | None = None) -> models_module.Config:
    parser = argparse.ArgumentParser(
        prog="claude-watchdog",
        description=(
            "Keep the Mac awake while Claude Code, Codex, OMX, or "
            "OpenCode works, then sleep."
        )
    )
    parser.add_argument("--version", action="version", version=f"claude-watchdog {VERSION}")
    parser.add_argument(
        "idle_minutes",
        nargs="?",
        type=_positive_float,
        default=30.0,
        help="idle threshold in minutes (default: 30)",
    )
    parser.add_argument(
        "--source",
        choices=models_module.SOURCE_CHOICES,
        default="auto",
        help=(
            "activity sources: auto (all), claude, codex, omx, "
            "opencode, or codex-omx (default: auto)"
        ),
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        help=(
            "Claude profile registry (default: "
            "~/.config/claude-watchdog/profiles.json when present)"
        ),
    )
    parser.add_argument(
        "--user-idle-minutes",
        type=_nonnegative_float,
        default=5.0,
        help="also require the user idle this long before sleeping; 0 disables (default: 5)",
    )
    parser.add_argument(
        "--select-window",
        type=_positive_float,
        default=900.0,
        help=(
            "admit sessions active within this many seconds at launch and during "
            "live discovery (default: 900)"
        ),
    )
    parser.add_argument(
        "--session-discovery",
        choices=("live", "frozen"),
        default="live",
        help=(
            "discover newly active sessions while running, or freeze at launch "
            "(default: live)"
        ),
    )
    parser.add_argument(
        "--poll",
        type=_positive_float,
        default=60.0,
        help="poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log the sleep decision but do not sleep",
    )
    parser.add_argument(
        "--display",
        choices=models_module.DISPLAY_CHOICES,
        default="auto",
        help="output mode: auto selects a dashboard on capable terminals (default: auto)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable dashboard colors (NO_COLOR is also honored)",
    )
    parser.add_argument(
        "--task-label",
        choices=models_module.TASK_LABEL_CHOICES,
        default="prompt",
        help=(
            "task text source: explicit/derived metadata only, or allow Claude's "
            "sanitized last-prompt fallback (default: prompt)"
        ),
    )
    parser.add_argument(
        "--hide",
        action="append",
        type=_hide_path,
        default=[],
        metavar="PATH",
        help=(
            "dismiss this activity file from this run's watch set; repeatable. "
            "Live discovery will not re-admit it"
        ),
    )
    args = parser.parse_args(argv)
    explicit_profiles_file = args.profiles_file is not None
    source_names = _source_names(args.source)
    if explicit_profiles_file and "claude" not in source_names:
        parser.error("--profiles-file requires --source auto or --source claude")
    try:
        profiles_file = (
            args.profiles_file.expanduser()
            if explicit_profiles_file else claude_profiles_path()
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise models_module.ActivityReadError(f"unable to expand Claude profiles file: {exc}") from exc
    try:
        profiles_file = profiles_file.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise models_module.ActivityReadError(
            f"unable to resolve Claude profiles file {profiles_file}: {exc}"
        ) from exc
    profiles = (
        load_claude_profiles(profiles_file, required=explicit_profiles_file)
        if "claude" in source_names else ()
    )
    return models_module.Config(
        idle_minutes=args.idle_minutes,
        user_idle_minutes=args.user_idle_minutes,
        select_window_seconds=args.select_window,
        poll_seconds=args.poll,
        source=args.source,
        session_discovery=args.session_discovery,
        dry_run=args.dry_run,
        display=args.display,
        no_color=args.no_color,
        task_label=args.task_label,
        claude_profiles=profiles,
        profiles_file=profiles_file,
        hidden_paths=frozenset(args.hide),
    )
