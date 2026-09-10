"""Read-only process confirmation for otherwise-unrecorded display lineage."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from . import models as models_module


Runner = Callable[..., subprocess.CompletedProcess[str]]
ExternalLineageLink = dict[str, object]


def read_bounded_json_object(path: Path, max_bytes: int) -> dict[str, object] | None:
    """Read one bounded JSON object, rejecting truncation and oversized files."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _parse_ps_clock(value: object, zone: tzinfo | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %Y")
        return parsed.astimezone() if zone is None else parsed.replace(tzinfo=zone)
    except (ValueError, OverflowError, OSError):
        return None


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _registry_matches(
    registry_dir: Path, session_ids: set[str]
) -> dict[str, list[dict[str, object]]]:
    matches: dict[str, list[dict[str, object]]] = {}
    try:
        entries: list[tuple[int, str]] = []
        with os.scandir(registry_dir) as iterator:
            for index, entry in enumerate(iterator):
                if index >= models_module.MAX_CLAUDE_REGISTRY_FILES:
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    entries.append((info.st_mtime_ns, entry.path))

        budget = models_module.MAX_CLAUDE_REGISTRY_BYTES
        for _, raw_path in sorted(entries, reverse=True):
            if budget <= 0:
                break
            try:
                limit = min(models_module.MAX_OMX_SESSION_BYTES, budget)
                with Path(raw_path).open("rb") as handle:
                    raw = handle.read(limit + 1)
                budget -= min(len(raw), limit)
                if len(raw) > limit:
                    continue
                value = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, ValueError, RecursionError):
                continue
            if not isinstance(value, dict):
                continue
            session_id = value.get("sessionId")
            if isinstance(session_id, str) and session_id in session_ids:
                matches.setdefault(session_id, []).append(value)
    except OSError:
        return {}
    return matches


def _omx_session(path: Path) -> tuple[int, str, datetime] | None:
    value = read_bounded_json_object(path, models_module.MAX_OMX_SESSION_BYTES)
    if value is None:
        return None
    pid = value.get("pid")
    native_session_id = value.get("native_session_id")
    started_at = _parse_utc_timestamp(value.get("started_at"))
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 1
        or not isinstance(native_session_id, str)
        or not native_session_id
        or started_at is None
    ):
        return None
    return pid, native_session_id, started_at


def _process_table(
    runner: Runner, local_timezone: tzinfo | None
) -> dict[int, tuple[int, datetime]]:
    try:
        completed = runner(
            ["/bin/ps", "-axo", "pid=,ppid=,lstart="],
            capture_output=True,
            text=True,
            timeout=models_module.PROCESS_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return {}
    if (
        not isinstance(completed, subprocess.CompletedProcess)
        or completed.returncode != 0
        or not isinstance(completed.stdout, str)
    ):
        return {}

    table: dict[int, tuple[int, datetime]] = {}
    for index, line in enumerate(completed.stdout.splitlines()):
        if index >= models_module.MAX_PROCESS_TABLE_ROWS:
            break
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        started = _parse_ps_clock(parts[2], local_timezone)
        if started is None:
            continue
        try:
            started_utc = started.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if pid in table:
            return {}
        table[pid] = (ppid, started_utc)
    return table


def _within_tolerance(
    first: datetime, second: datetime, tolerance_seconds: float
) -> bool:
    return abs((first - second).total_seconds()) <= tolerance_seconds


def _has_ancestor(
    child_pid: int,
    ancestor_pid: int,
    table: Mapping[int, tuple[int, datetime]],
) -> bool:
    current = child_pid
    seen: set[int] = set()
    for _ in range(models_module.MAX_PROCESS_ANCESTRY_HOPS):
        if current in seen:
            return False
        seen.add(current)
        process = table.get(current)
        if process is None:
            return False
        parent_pid = process[0]
        if parent_pid <= 1:
            return False
        if parent_pid == ancestor_pid:
            return True
        current = parent_pid
    return False


def discover_process_lineage(
    metadata: Mapping[tuple[str, str], models_module.SessionMetadata],
    registry_dir: Path,
    *,
    runner: Runner = subprocess.run,
    observed_at: datetime | None = None,
    local_timezone: tzinfo | None = None,
) -> tuple[ExternalLineageLink, ...]:
    """Confirm Claude children from provider records plus live process ancestry."""
    child_ids = {
        value.session_id
        for key, value in metadata.items()
        if key[0] == "claude"
        and value.session_id != models_module.UNKNOWN
        and value.parent_session_id == models_module.UNKNOWN
        and value.external_parent_key is None
    }
    if not child_ids:
        return ()

    registry_matches = _registry_matches(registry_dir, child_ids)
    candidates: list[tuple[str, int, datetime, int, str, datetime]] = []
    for child_id, matches in registry_matches.items():
        if len(matches) != 1:
            continue
        record = matches[0]
        child_pid = record.get("pid")
        cwd = record.get("cwd")
        if (
            not isinstance(child_pid, int)
            or isinstance(child_pid, bool)
            or child_pid <= 1
            or not isinstance(cwd, str)
            or not Path(cwd).is_absolute()
        ):
            continue
        child_started = _parse_ps_clock(record.get("procStart"), timezone.utc)
        if child_started is None:
            continue
        omx_session = _omx_session(Path(cwd) / ".omx" / "state" / "session.json")
        if omx_session is None:
            continue
        omx_pid, native_session_id, omx_started = omx_session
        candidates.append(
            (
                child_id,
                child_pid,
                child_started,
                omx_pid,
                native_session_id,
                omx_started,
            )
        )
    if not candidates:
        return ()

    table = _process_table(runner, local_timezone)
    observation = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    links: list[ExternalLineageLink] = []
    for (
        child_id,
        child_pid,
        child_started,
        omx_pid,
        native_session_id,
        omx_started,
    ) in candidates:
        child_process = table.get(child_pid)
        omx_process = table.get(omx_pid)
        if child_process is None or omx_process is None:
            continue
        if not _within_tolerance(
            child_started,
            child_process[1],
            models_module.CLAUDE_PROCESS_START_TOLERANCE_SECONDS,
        ):
            continue
        if not _within_tolerance(
            omx_started,
            omx_process[1],
            models_module.OMX_PROCESS_START_TOLERANCE_SECONDS,
        ):
            continue
        if not _has_ancestor(child_pid, omx_pid, table):
            continue
        links.append(
            {
                "child": {"source": "claude", "session_id": child_id},
                "parent": {
                    "source": "codex",
                    "session_id": native_session_id,
                },
                "evidence": (
                    f"process-confirmed Claude pid {child_pid} descended from OMX pid "
                    f"{omx_pid}, observed {observation.isoformat()}; parent identity is "
                    "session.json native_session_id and may render shallower than the OMX leader"
                ),
            }
        )
    return tuple(links)
