"""Persisted activity discovery, admission, and timestamp readers."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import models as models_module

def _scandir(directory: Path, source: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise models_module.ActivityReadError(
            f"unable to discover {source} activity under {directory}: {exc}"
        ) from exc


def _entry_is_dir(
    entry: os.DirEntry[str], source: str, *, follow_symlinks: bool
) -> bool:
    try:
        return entry.is_dir(follow_symlinks=follow_symlinks)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise models_module.ActivityReadError(
            f"unable to inspect {source} activity path {entry.path}: {exc}"
        ) from exc


def _entry_is_file(entry: os.DirEntry[str], source: str) -> bool:
    try:
        return entry.is_file(follow_symlinks=True)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise models_module.ActivityReadError(
            f"unable to inspect {source} activity path {entry.path}: {exc}"
        ) from exc


def _two_level_jsonl(directory: Path, source: str) -> list[Path]:
    paths: list[Path] = []
    for project in _scandir(directory, source):
        if not _entry_is_dir(project, source, follow_symlinks=True):
            continue
        for entry in _scandir(Path(project.path), source):
            if entry.name.endswith(".jsonl") and _entry_is_file(entry, source):
                paths.append(Path(entry.path))
            elif _entry_is_dir(entry, source, follow_symlinks=False):
                # Claude stores child transcripts beside the parent's JSONL:
                # project/session-id/subagents/agent-id.jsonl.
                for child_dir in _scandir(Path(entry.path), source):
                    if child_dir.name != "subagents" or not _entry_is_dir(
                        child_dir, source, follow_symlinks=False
                    ):
                        continue
                    for child in _scandir(Path(child_dir.path), source):
                        if (child.name.startswith("agent-")
                                and child.name.endswith(".jsonl")
                                and _entry_is_file(child, source)):
                            paths.append(Path(child.path))
    return sorted(paths)


def _recursive_codex_jsonl(directory: Path) -> list[Path]:
    paths: list[Path] = []
    pending = [directory]
    while pending:
        current = pending.pop()
        for entry in _scandir(current, "codex"):
            if _entry_is_dir(entry, "codex", follow_symlinks=False):
                pending.append(Path(entry.path))
            elif (
                entry.name.startswith("rollout-")
                and entry.name.endswith(".jsonl")
                and _entry_is_file(entry, "codex")
            ):
                paths.append(Path(entry.path))
    return sorted(paths)


def _paths_for_source(source: str) -> list[Path]:
    if source == "claude":
        return _two_level_jsonl(config_module.claude_projects_dir(), source)
    if source == "codex":
        return _recursive_codex_jsonl(config_module.codex_sessions_dir())
    if source == "omx":
        paths: list[Path] = []
        seen: set[str] = set()
        for directory in config_module.omx_log_dirs():
            for entry in _scandir(directory, source):
                if not entry.name.endswith(".jsonl") or not _entry_is_file(entry, source):
                    continue
                path = Path(entry.path)
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    paths.append(path)
        return paths
    if source == "opencode":
        path = config_module.opencode_database_path()
        if path is None:
            return []
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise models_module.ActivityReadError(
                f"unable to inspect OpenCode database {path}: {exc}"
            ) from exc
        return [path] if stat.S_ISREG(mode) else []
    raise ValueError(f"unsupported source: {source!r}")


def activity_files(
    source: str = "auto", claude_profiles: tuple[models_module.ClaudeProfile, ...] = ()
) -> list[models_module.ActivityFile]:
    """Discover and size-snapshot paths for the requested source selection."""
    files: list[models_module.ActivityFile] = []
    seen: set[tuple[str, str]] = set()
    for source_name in config_module._source_names(source):
        try:
            discovered = [(path, None) for path in _paths_for_source(source_name)]
            if source_name == "claude":
                for profile in claude_profiles:
                    discovered.extend(
                        (path, profile)
                        for path in _two_level_jsonl(profile.projects_dir, source_name)
                    )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise models_module.ActivityReadError(
                f"unable to discover {source_name} activity: {exc}"
            ) from exc
        for path, profile in discovered:
            try:
                canonical_path = path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as exc:
                raise models_module.ActivityReadError(
                    f"unable to resolve activity file {path}: {exc}"
                ) from exc
            key = (source_name, str(canonical_path))
            if key in seen:
                continue
            try:
                snapshot_size = path.stat().st_size
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise models_module.ActivityReadError(
                    f"unable to stat activity file {path}: {exc}"
                ) from exc
            seen.add(key)
            files.append(
                models_module.ActivityFile(
                    path=path,
                    source=source_name,
                    snapshot_size=snapshot_size,
                    profile_id=None if profile is None else profile.id,
                    profile_label=None if profile is None else profile.label,
                )
            )
    return files


def transcripts() -> list[Path]:
    """Backward-compatible Claude transcript listing."""
    return [item.path for item in activity_files("claude")]


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    if not isinstance(value, str):
        return None
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _reverse_lines(
    path: Path,
    *,
    end_offset: int | None = None,
    chunk_bytes: int = models_module.READ_CHUNK_BYTES,
) -> Iterator[str]:
    """Yield complete lines newest-first, expanding across arbitrarily large lines."""
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        position = size if end_offset is None else min(size, max(0, end_offset))
        carry = b""

        while position > 0:
            read_size = min(chunk_bytes, position)
            position -= read_size
            fh.seek(position)
            data = fh.read(read_size) + carry
            parts = data.split(b"\n")
            carry = parts[0]
            for raw_line in reversed(parts[1:]):
                yield raw_line.decode("utf-8", errors="replace")

        if carry:
            yield carry.decode("utf-8", errors="replace")


def _json_objects_reverse(
    path: Path, *, end_offset: int | None = None
) -> Iterator[dict[str, object]]:
    for line in _reverse_lines(path, end_offset=end_offset):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _record_identifiers(obj: dict[str, object]) -> frozenset[str]:
    return frozenset(
        value
        for field in models_module.OMX_ID_FIELDS
        if isinstance((value := obj.get(field)), str) and value
    )


def _record_timestamp(obj: dict[str, object], now: datetime) -> datetime | None:
    value = obj.get("timestamp")
    parsed = _parse_ts(value) if isinstance(value, str) else None
    if parsed is None:
        return None
    if (parsed - now).total_seconds() > models_module.MAX_FUTURE_SKEW_SECONDS:
        return None
    return parsed


def last_activity(
    path: Path,
    *,
    now: datetime | None = None,
    end_offset: int | None = None,
    identities: frozenset[str] | None = None,
) -> datetime | None:
    """Return the newest attributable, sane top-level JSONL timestamp."""
    now = now or datetime.now(timezone.utc)
    freshest: datetime | None = None
    try:
        for obj in _json_objects_reverse(path, end_offset=end_offset):
            if identities is not None and not (_record_identifiers(obj) & identities):
                continue
            timestamp = _record_timestamp(obj, now)
            if timestamp is not None:
                if identities is None:
                    return timestamp
                if freshest is None or timestamp > freshest:
                    freshest = timestamp
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise models_module.ActivityReadError(
            f"unable to read activity file {path}: {exc}"
        ) from exc
    return freshest


def _activity_age(now: datetime, timestamp: datetime) -> float | None:
    age = (now - timestamp).total_seconds()
    if age < -models_module.MAX_FUTURE_SKEW_SECONDS:
        return None
    return max(0.0, age)


def _timestamp_from_epoch_millis(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _opencode_activity(
    path: Path,
    now: datetime,
    identities: frozenset[str] | None = None,
) -> list[tuple[str, datetime]]:
    upper_millis = int(
        (now.timestamp() + models_module.MAX_FUTURE_SKEW_SECONDS) * 1000
    )
    lineage_cte = ""
    identity_filter = ""
    parameters: list[object] = [upper_millis]
    if identities is not None:
        if not identities:
            return []
        roots = sorted(identities)
        seeds = ", ".join("(?)" for _ in roots)
        lineage_cte = f"""
            WITH RECURSIVE watched(session_id) AS (
                VALUES {seeds}
                UNION
                SELECT child.id
                FROM session AS child
                JOIN watched AS parent ON child.parent_id = parent.session_id
            )
        """
        identity_filter = " AND session_id IN (SELECT session_id FROM watched)"
        parameters = [*roots, upper_millis]

    query = f"""{lineage_cte}
        SELECT session_id, MAX(time_updated)
        FROM (
            SELECT session_id, time_updated FROM message
            UNION ALL
            SELECT session_id, time_updated FROM part
        )
        WHERE time_updated <= ?{identity_filter}
        GROUP BY session_id
    """
    try:
        database = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            database.execute("PRAGMA query_only = ON")
            rows = database.execute(query, parameters).fetchall()
        finally:
            database.close()
    except sqlite3.Error as exc:
        raise models_module.OpenCodeDatabaseError(
            f"unable to read OpenCode activity database {path}: {exc}"
        ) from exc

    activity: list[tuple[str, datetime]] = []
    for identity, value in rows:
        if not isinstance(identity, str) or not identity:
            continue
        timestamp = _timestamp_from_epoch_millis(value)
        if timestamp is not None:
            activity.append((identity, timestamp))
    return activity


def _freeze_opencode_item(
    item: models_module.ActivityFile, cfg: models_module.Config, now: datetime
) -> models_module.ActivityFile | None:
    identities = {
        identity
        for identity, timestamp in _opencode_activity(item.path, now)
        if (age := _activity_age(now, timestamp)) is not None
        and age <= cfg.select_window_seconds
    }
    if not identities:
        return None
    _opencode_activity(item.path, now, frozenset(identities))
    return models_module.ActivityFile(
        path=item.path,
        source=item.source,
        snapshot_size=item.snapshot_size,
        identities=frozenset(identities),
    )


def _freeze_omx_item(item: models_module.ActivityFile, cfg: models_module.Config, now: datetime) -> models_module.ActivityFile | None:
    """Capture session identities already present in a shared OMX log at launch."""
    identities: set[str] = set()
    try:
        for obj in _json_objects_reverse(item.path, end_offset=item.snapshot_size):
            timestamp = _record_timestamp(obj, now)
            if timestamp is None:
                continue
            age = _activity_age(now, timestamp)
            if age is None:
                continue
            if age > cfg.select_window_seconds:
                continue
            identities.update(_record_identifiers(obj))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise models_module.ActivityReadError(
            f"unable to read OMX activity file {item.path}: {exc}"
        ) from exc

    if not identities:
        return None
    return models_module.ActivityFile(
        path=item.path,
        source=item.source,
        snapshot_size=item.snapshot_size,
        identities=frozenset(identities),
    )


def hidden_path_key(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return path


def is_hidden_path(cfg: models_module.Config, path: Path) -> bool:
    return hidden_path_key(path) in cfg.hidden_paths


def drop_hidden_targets(
    cfg: models_module.Config,
    items: Iterable[models_module.ActivityFile],
) -> list[models_module.ActivityFile]:
    return [item for item in items if not is_hidden_path(cfg, item.path)]


def select_watch_set(
    cfg: models_module.Config,
    candidates: Iterable[models_module.ActivityFile] | None = None,
    *,
    now: datetime | None = None,
) -> list[models_module.ActivityFile]:
    """Select active files from one launch-time path and size snapshot."""
    now = now or datetime.now(timezone.utc)
    candidates = (
        (
            activity_files(cfg.source, cfg.claude_profiles)
            if cfg.claude_profiles else activity_files(cfg.source)
        )
        if candidates is None
        else candidates
    )
    watch: list[models_module.ActivityFile] = []

    for item in candidates:
        if is_hidden_path(cfg, item.path):
            continue
        if item.source == "opencode":
            frozen_item = _freeze_opencode_item(item, cfg, now)
            if frozen_item is not None:
                watch.append(frozen_item)
            continue
        if item.source == "omx":
            frozen_item = _freeze_omx_item(item, cfg, now)
            if frozen_item is not None:
                watch.append(frozen_item)
            continue

        timestamp = last_activity(
            item.path,
            now=now,
            end_offset=item.snapshot_size,
        )
        if timestamp is None:
            continue
        age = _activity_age(now, timestamp)
        if age is not None and age <= cfg.select_window_seconds:
            watch.append(item)

    return watch


def refresh_watch_set(
    cfg: models_module.Config,
    current: list[models_module.ActivityFile],
    candidates: Iterable[models_module.ActivityFile] | None = None,
    now: datetime | None = None,
) -> list[models_module.ActivityFile]:
    """Return an additive watch-set proposal from one complete discovery snapshot."""
    now = now or datetime.now(timezone.utc)
    try:
        snapshot = (
            (
                activity_files(cfg.source, cfg.claude_profiles)
                if cfg.claude_profiles else activity_files(cfg.source)
            )
            if candidates is None else list(candidates)
        )
        allowed_sources = set(config_module._source_names(cfg.source))
        eligible = select_watch_set(
            cfg,
            (item for item in snapshot if item.source in allowed_sources),
            now=now,
        )
    except models_module.WatchdogError:
        raise
    except Exception as exc:
        raise models_module.WatchdogError(f"unable to refresh activity targets: {exc}") from exc

    proposed = drop_hidden_targets(cfg, current)
    index = {
        (item.source, item.path): position
        for position, item in enumerate(proposed)
    }
    for item in eligible:
        key = (item.source, item.path)
        position = index.get(key)
        if position is None:
            index[key] = len(proposed)
            proposed.append(item)
            continue
        existing = proposed[position]
        if item.source not in {"omx", "opencode"}:
            continue
        identities = existing.identities | item.identities
        if identities != existing.identities:
            proposed[position] = models_module.ActivityFile(
                path=existing.path,
                source=existing.source,
                snapshot_size=existing.snapshot_size,
                identities=identities,
            )
    return proposed


def _last_activity_for(item: models_module.ActivityFile, now: datetime) -> datetime | None:
    if item.source == "opencode":
        activity = _opencode_activity(item.path, now, item.identities)
        return max((timestamp for _, timestamp in activity), default=None)
    if item.source == "omx":
        if not item.identities:
            return None
        return last_activity(item.path, now=now, identities=item.identities)
    return last_activity(item.path, now=now)
