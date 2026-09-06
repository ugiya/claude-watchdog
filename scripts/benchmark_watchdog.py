#!/usr/bin/env python3
"""Benchmark live refresh for isolated Codex, OMX, and OpenCode workloads."""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


CODEX_FILE_COUNT = 15_000
CODEX_RECENT_COUNT = 20
OMX_RECORD_COUNT = 100_000
OMX_RECENT_IDENTITIES = 8
OPENCODE_SESSION_COUNT = 5_000
OPENCODE_ROOT_COUNT = 50
REFRESH_PASSES = 5
POLL_SECONDS = 60.0
MAX_MEDIAN_SECONDS = 5.0
MAX_SINGLE_PASS_SECONDS = 10.0


def _load_target(path: Path):
    loader = importlib.machinery.SourceFileLoader("benchmark_watchdog_target", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"unable to load target: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def _write_codex_tree(root: Path, now: datetime) -> None:
    recent = json.dumps({"timestamp": now.isoformat()}) + "\n"
    old = json.dumps({"timestamp": (now - timedelta(days=1)).isoformat()}) + "\n"
    for index in range(CODEX_FILE_COUNT):
        directory = root / f"bucket-{index // 1000:02d}"
        directory.mkdir(exist_ok=True)
        payload = recent if index < CODEX_RECENT_COUNT else old
        (directory / f"rollout-{index:05d}.jsonl").write_text(payload, encoding="utf-8")


def _write_omx_log(path: Path, now: datetime) -> None:
    """Write 100k records ending in a stale record after recent identities."""
    path.parent.mkdir(parents=True)
    old_timestamp = (now - timedelta(days=1)).isoformat()
    recent_timestamp = (now - timedelta(seconds=1)).isoformat()
    old_count = OMX_RECORD_COUNT - OMX_RECENT_IDENTITIES - 1
    with path.open("w", encoding="utf-8") as handle:
        for index in range(old_count):
            handle.write(
                json.dumps(
                    {
                        "timestamp": old_timestamp,
                        "thread_id": f"stale-{index % 32:02d}",
                    }
                )
                + "\n"
            )
        for index in range(OMX_RECENT_IDENTITIES):
            handle.write(
                json.dumps(
                    {
                        "timestamp": recent_timestamp,
                        "thread_id": f"recent-{index:02d}",
                    }
                )
                + "\n"
            )
        handle.write(
            json.dumps({"timestamp": old_timestamp, "thread_id": "old-tail"}) + "\n"
        )


def _epoch_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _write_opencode_database(path: Path, now: datetime) -> sqlite3.Connection:
    """Create 50 roots with 99 recursively linked descendants apiece."""
    path.parent.mkdir(parents=True)
    database = sqlite3.connect(path)
    database.execute("PRAGMA journal_mode = WAL")
    database.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT)")
    database.execute(
        "CREATE TABLE message (session_id TEXT NOT NULL, time_updated INTEGER NOT NULL)"
    )
    database.execute(
        "CREATE TABLE part (session_id TEXT NOT NULL, time_updated INTEGER NOT NULL)"
    )
    session_rows: list[tuple[str, str | None]] = []
    part_rows: list[tuple[str, int]] = []
    old_millis = _epoch_millis(now - timedelta(days=1))
    recent_millis = _epoch_millis(now - timedelta(seconds=1))
    descendants_per_root = OPENCODE_SESSION_COUNT // OPENCODE_ROOT_COUNT - 1
    for root_index in range(OPENCODE_ROOT_COUNT):
        root = f"root-{root_index:03d}"
        session_rows.append((root, None))
        part_rows.append((root, recent_millis))
        parent = root
        for child_index in range(1, descendants_per_root + 1):
            child = f"{root}-child-{child_index:03d}"
            session_rows.append((child, parent))
            part_rows.append((child, old_millis))
            parent = child
    database.executemany(
        "INSERT INTO session (id, parent_id) VALUES (?, ?)", session_rows
    )
    database.executemany(
        "INSERT INTO part (session_id, time_updated) VALUES (?, ?)", part_rows
    )
    database.commit()
    return database


def _refresh_passes(refresh, cfg, watch_set, now: datetime):
    durations: list[float] = []
    for _ in range(REFRESH_PASSES):
        started = time.perf_counter()
        watch_set = refresh(cfg, watch_set, candidates=None, now=now)
        durations.append(time.perf_counter() - started)
        if not isinstance(watch_set, list):
            raise TypeError("refresh_watch_set must return the updated watch-set list")
    return watch_set, durations


def _summarize(durations: list[float]) -> dict[str, object]:
    median_seconds = statistics.median(durations)
    max_seconds = max(durations)
    return {
        "refresh_passes": REFRESH_PASSES,
        "poll_seconds": POLL_SECONDS,
        "durations_seconds": [round(value, 6) for value in durations],
        "median_seconds": round(median_seconds, 6),
        "max_seconds": round(max_seconds, 6),
        "max_as_poll_fraction": round(max_seconds / POLL_SECONDS, 6),
        "passed": (
            median_seconds < MAX_MEDIAN_SECONDS
            and max_seconds < MAX_SINGLE_PASS_SECONDS
        ),
    }


def _benchmark_codex(watchdog, refresh, root: Path, now: datetime) -> dict[str, object]:
    sessions = root / "codex-sessions"
    sessions.mkdir()
    _write_codex_tree(sessions, now)
    cfg = watchdog.Config(
        source="codex",
        select_window_seconds=30,
        poll_seconds=POLL_SECONDS,
        session_discovery="live",
    )
    with mock.patch.object(watchdog, "codex_sessions_dir", return_value=sessions):
        candidates = watchdog.activity_files("codex")
        watch_set = watchdog.select_watch_set(cfg, candidates, now=now)
        if len(watch_set) != CODEX_RECENT_COUNT:
            raise AssertionError(
                f"Codex fixture selected {len(watch_set)} files; "
                f"expected {CODEX_RECENT_COUNT}"
            )
        _, durations = _refresh_passes(refresh, cfg, watch_set, now)
    return {
        "files": CODEX_FILE_COUNT,
        "recent_files": CODEX_RECENT_COUNT,
        **_summarize(durations),
    }


def _benchmark_omx(watchdog, refresh, root: Path, now: datetime) -> dict[str, object]:
    log_directory = root / "omx-logs"
    log_path = log_directory / "turns.jsonl"
    _write_omx_log(log_path, now)
    cfg = watchdog.Config(
        source="omx",
        select_window_seconds=30,
        poll_seconds=POLL_SECONDS,
        session_discovery="live",
    )
    with mock.patch.object(watchdog, "omx_log_dirs", return_value=[log_directory]):
        candidates = watchdog.activity_files("omx")
        watch_set = watchdog.select_watch_set(cfg, candidates, now=now)
        if len(watch_set) != 1:
            raise AssertionError(f"OMX fixture selected {len(watch_set)} logs; expected 1")
        if len(watch_set[0].identities) != OMX_RECENT_IDENTITIES:
            raise AssertionError(
                f"OMX fixture selected {len(watch_set[0].identities)} identities; "
                f"expected {OMX_RECENT_IDENTITIES}"
            )
        _, durations = _refresh_passes(refresh, cfg, watch_set, now)
    return {
        "records": OMX_RECORD_COUNT,
        "recent_identities": OMX_RECENT_IDENTITIES,
        "stale_out_of_order_tail": True,
        **_summarize(durations),
    }


def _benchmark_opencode(
    watchdog, refresh, root: Path, now: datetime
) -> dict[str, object]:
    database_path = root / "opencode" / "opencode.db"
    database = _write_opencode_database(database_path, now)
    try:
        cfg = watchdog.Config(
            source="opencode",
            select_window_seconds=30,
            poll_seconds=POLL_SECONDS,
            session_discovery="live",
        )
        with mock.patch.object(
            watchdog, "opencode_database_path", return_value=database_path
        ):
            candidates = watchdog.activity_files("opencode")
            watch_set = watchdog.select_watch_set(cfg, candidates, now=now)
            if len(watch_set) != 1:
                raise AssertionError(
                    f"OpenCode fixture selected {len(watch_set)} databases; expected 1"
                )
            if len(watch_set[0].identities) != OPENCODE_ROOT_COUNT:
                raise AssertionError(
                    f"OpenCode fixture selected {len(watch_set[0].identities)} roots; "
                    f"expected {OPENCODE_ROOT_COUNT}"
                )

            recent_millis = _epoch_millis(now - timedelta(seconds=1))
            deepest_descendants = [
                (f"root-{index:03d}-child-099", recent_millis)
                for index in range(OPENCODE_ROOT_COUNT)
            ]
            database.executemany(
                "INSERT INTO message (session_id, time_updated) VALUES (?, ?)",
                deepest_descendants,
            )
            database.commit()
            _, durations = _refresh_passes(refresh, cfg, watch_set, now)
    finally:
        database.close()
    return {
        "sessions": OPENCODE_SESSION_COUNT,
        "lineage_roots": OPENCODE_ROOT_COUNT,
        "descendants_per_root": OPENCODE_SESSION_COUNT // OPENCODE_ROOT_COUNT - 1,
        **_summarize(durations),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "claude-watchdog",
        help="worktree claude-watchdog executable to import",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optionally write the JSON result to this artifact path",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    target = args.target.resolve(strict=True)
    watchdog = _load_target(target)
    refresh = getattr(watchdog, "refresh_watch_set", None)
    if refresh is None:
        raise SystemExit("target does not expose refresh_watch_set")

    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="watchdog-benchmark-") as temporary:
        root = Path(temporary)
        workloads = {
            "codex": _benchmark_codex(watchdog, refresh, root, now),
            "omx": _benchmark_omx(watchdog, refresh, root, now),
            "opencode": _benchmark_opencode(watchdog, refresh, root, now),
        }

    passed = all(bool(workload["passed"]) for workload in workloads.values())
    result = {
        "target": str(target),
        "python": sys.version,
        "platform": platform.platform(),
        "poll_seconds": POLL_SECONDS,
        "limits": {
            "median_seconds": MAX_MEDIAN_SECONDS,
            "single_pass_seconds": MAX_SINGLE_PASS_SECONDS,
        },
        "workloads": workloads,
        "passed": passed,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
