#!/usr/bin/env python3
"""Regression tests for the personal claude-watchdog executable."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


from claude_watchdog import models as wd_models
from claude_watchdog import config as wd_config
from claude_watchdog import activity as wd_activity
from claude_watchdog import power as wd_power
from claude_watchdog import app as wd_app


def _write_records(path: Path, *records: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _item(path: Path, source: str = "codex"):
    return wd_models.ActivityFile(
        path=path,
        source=source,
        snapshot_size=path.stat().st_size,
    )


def _epoch_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _open_opencode_fixture(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path)
    database.execute("PRAGMA journal_mode = WAL")
    database.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT)"
    )
    database.execute(
        "CREATE TABLE message (session_id TEXT NOT NULL, time_updated INTEGER NOT NULL)"
    )
    database.execute(
        "CREATE TABLE part (session_id TEXT NOT NULL, time_updated INTEGER NOT NULL)"
    )
    database.commit()
    return database


class ClaudeProfileTests(unittest.TestCase):
    def _write_config(self, path: Path, profiles: list[dict[str, object]]) -> None:
        path.write_text(
            json.dumps({"version": 1, "claude_profiles": profiles}),
            encoding="utf-8",
        )

    def test_loads_bounded_versioned_profile_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "profiles.json"
            projects = root / "work" / "projects"
            self._write_config(
                path,
                [{"id": "work", "label": "Work", "projects_dir": str(projects)}],
            )
            profiles = wd_config.load_claude_profiles(path, required=True)
        self.assertEqual(
            profiles,
            (wd_models.ClaudeProfile("work", "Work", projects.resolve()),),
        )

    def test_profile_label_defaults_to_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "claude_profiles": [
                            {"id": "work", "projects_dir": str(root / "projects")}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            profile = wd_config.load_claude_profiles(path, required=True)[0]
        self.assertEqual(profile.label, "work")

    def test_missing_default_is_empty_but_missing_explicit_path_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(
                wd_config.load_claude_profiles(missing, required=False), ()
            )
            with self.assertRaisesRegex(wd_models.ActivityReadError, "does not exist"):
                wd_config.load_claude_profiles(missing, required=True)

    def test_rejects_oversized_or_malformed_profile_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (wd_models.MAX_PROFILES_BYTES + 1))
            with self.assertRaisesRegex(wd_models.ActivityReadError, "64 KiB"):
                wd_config.load_claude_profiles(oversized, required=True)

            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(wd_models.ActivityReadError, "valid JSON"):
                wd_config.load_claude_profiles(malformed, required=True)

            boolean_version = root / "boolean-version.json"
            boolean_version.write_text(
                json.dumps({"version": True, "claude_profiles": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(wd_models.ActivityReadError, "version must be 1"):
                wd_config.load_claude_profiles(boolean_version, required=True)

    def test_rejects_duplicate_ids_roots_builtin_alias_and_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "profiles.json"
            cases = (
                [
                    {"id": "work", "label": "Work", "projects_dir": str(root / "a")},
                    {"id": "work", "label": "Other", "projects_dir": str(root / "b")},
                ],
                [
                    {"id": "work", "label": "Work", "projects_dir": str(root / "a")},
                    {"id": "other", "label": "Other", "projects_dir": str(root / "a" / ".." / "a")},
                ],
                [{"id": "not a slug", "label": "Work", "projects_dir": str(root / "a")}],
                [{"id": "work", "label": "", "projects_dir": str(root / "a")}],
                [{"id": "work", "label": "界" * 33, "projects_dir": str(root / "a")}],
                [{"id": "work", "label": "Line\u2028Break", "projects_dir": str(root / "a")}],
                [{"id": "work", "label": "Work", "projects_dir": "relative/projects"}],
                [{"id": "work", "label": "Work", "projects_dir": "~watchdog-profile-user-does-not-exist/projects"}],
            )
            for profiles in cases:
                with self.subTest(profiles=profiles):
                    self._write_config(path, profiles)
                    with self.assertRaises(wd_models.ActivityReadError):
                        wd_config.load_claude_profiles(path, required=True)

            builtin = root / "builtin"
            self._write_config(
                path,
                [{"id": "work", "label": "Work", "projects_dir": str(builtin)}],
            )
            with mock.patch.object(wd_config, "claude_projects_dir", return_value=builtin):
                with self.assertRaisesRegex(wd_models.ActivityReadError, "built-in"):
                    wd_config.load_claude_profiles(path, required=True)

    def test_rejects_more_than_32_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "profiles.json"
            self._write_config(
                path,
                [
                    {"id": f"p{index}", "label": f"P {index}", "projects_dir": str(root / f"p{index}")}
                    for index in range(wd_models.MAX_CLAUDE_PROFILES + 1)
                ],
            )
            with self.assertRaisesRegex(wd_models.ActivityReadError, "at most 32"):
                wd_config.load_claude_profiles(path, required=True)


class TimestampAndTailTests(unittest.TestCase):
    def test_timestamp_parser_normalizes_offsets(self):
        self.assertEqual(
            wd_activity._parse_ts("2026-08-14T03:00:00+03:00"),
            datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIsNone(wd_activity._parse_ts("not-a-timestamp"))

    def test_complete_record_larger_than_read_chunk_is_parsed(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        recent = now - timedelta(seconds=10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-large.jsonl"
            _write_records(
                path,
                {"timestamp": (now - timedelta(hours=1)).isoformat()},
                {
                    "timestamp": recent.isoformat(),
                    "payload": "x" * (wd_models.READ_CHUNK_BYTES * 4),
                },
            )
            self.assertEqual(wd_activity.last_activity(path, now=now), recent)

    def test_malformed_partial_tail_falls_back_to_last_complete_record(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        recent = now - timedelta(seconds=10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.jsonl"
            _write_records(path, {"timestamp": recent.isoformat()})
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"timestamp":"unterminated')
            self.assertEqual(wd_activity.last_activity(path, now=now), recent)

    def test_missing_activity_file_is_quiet(self):
        self.assertIsNone(
            wd_activity.last_activity(Path("/definitely/missing.jsonl"))
        )

    def test_activity_read_permission_error_fails_safe(self):
        with mock.patch.object(wd_activity, "_json_objects_reverse",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaisesRegex(wd_models.ActivityReadError, "denied"):
                wd_activity.last_activity(Path("denied.jsonl"))

    def test_far_future_timestamp_is_ignored(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        recent = now - timedelta(seconds=10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "future.jsonl"
            _write_records(
                path,
                {"timestamp": recent.isoformat()},
                {"timestamp": (now + timedelta(days=30)).isoformat()},
            )
            self.assertEqual(wd_activity.last_activity(path, now=now), recent)


class DiscoveryAndSelectionTests(unittest.TestCase):
    def test_disappearing_file_during_discovery_is_ignored(self):
        path = Path("gone.jsonl")
        with (
            mock.patch.object(wd_activity, "_paths_for_source", return_value=[path]),
            mock.patch.object(Path, "stat", side_effect=FileNotFoundError("gone")),
        ):
            self.assertEqual(wd_activity.activity_files("codex"), [])

    def test_discovery_stat_permission_error_fails_safe(self):
        path = Path("denied.jsonl")
        with (
            mock.patch.object(wd_activity, "_paths_for_source", return_value=[path]),
            mock.patch.object(Path, "stat", side_effect=PermissionError("denied")),
        ):
            with self.assertRaisesRegex(wd_models.ActivityReadError, "denied"):
                wd_activity.activity_files("codex")

    def test_provider_discovery_error_fails_safe(self):
        with mock.patch.object(wd_activity, "_paths_for_source",
            side_effect=PermissionError("provider denied"),
        ):
            with self.assertRaisesRegex(wd_models.ActivityReadError, "provider denied"):
                wd_activity.activity_files("codex")

    def test_source_union_and_filters(self):
        now = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "claude"
            profile_root = root / "work-claude" / "projects"
            codex = root / "codex"
            omx = root / "omx"
            opencode = root / "opencode.db"
            _write_records(claude / "project" / "session.jsonl", {"timestamp": now})
            _write_records(profile_root / "project" / "profile.jsonl", {"timestamp": now})
            _write_records(codex / "rollout-session.jsonl", {"timestamp": now})
            _write_records(
                omx / "turns.jsonl",
                {"timestamp": now, "thread_id": "thread-a"},
            )
            opencode.write_bytes(b"")

            with (
                mock.patch.object(wd_config, "claude_projects_dir", return_value=claude),
                mock.patch.object(wd_config, "codex_sessions_dir", return_value=codex),
                mock.patch.object(wd_config, "omx_log_dirs", return_value=[omx]),
                mock.patch.object(wd_config, "opencode_database_path", return_value=opencode),
            ):
                profiles = (wd_models.ClaudeProfile("work", "Work", profile_root),)
                auto = wd_activity.activity_files("auto", profiles)
                self.assertEqual(
                    {item.source for item in auto},
                    {"claude", "codex", "omx", "opencode"},
                )
                claude_files = wd_activity.activity_files("claude", profiles)
                self.assertEqual([item.source for item in claude_files], ["claude", "claude"])
                self.assertEqual(
                    [(item.profile_id, item.profile_label) for item in claude_files],
                    [(None, None), ("work", "Work")],
                )
                self.assertEqual([item.source for item in wd_activity.activity_files("codex")], ["codex"])
                self.assertEqual([item.source for item in wd_activity.activity_files("omx")], ["omx"])
                self.assertEqual(
                    [item.source for item in wd_activity.activity_files("opencode")],
                    ["opencode"],
                )
                self.assertNotIn("claude", {item.source for item in wd_activity.activity_files("codex-omx")})
                self.assertTrue(all(item.snapshot_size is not None for item in auto))

    def test_auto_selects_recent_profile_transcript(self):
        now = datetime(2026, 8, 16, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / "claude"
            claude.mkdir()
            projects = root / "work-claude" / "projects"
            transcript = projects / "project" / "session.jsonl"
            _write_records(transcript, {"timestamp": now.isoformat()})
            with (
                mock.patch.object(wd_config, "claude_projects_dir", return_value=claude),
                mock.patch.object(wd_config, "codex_sessions_dir", return_value=root / "codex"),
                mock.patch.object(wd_config, "omx_log_dirs", return_value=[root / "omx"]),
                mock.patch.object(wd_config, "opencode_database_path", return_value=None),
            ):
                selected = wd_activity.select_watch_set(
                    wd_models.Config(
                        select_window_seconds=120,
                        source="auto",
                        claude_profiles=(wd_models.ClaudeProfile("work", "Work", projects),),
                    ),
                    now=now,
                )
            self.assertEqual(
                [(item.source, item.profile_id, item.path) for item in selected],
                [("claude", "work", transcript)],
            )

    def test_profile_discovery_is_scoped_to_configured_root(self):
        now = datetime(2026, 8, 16, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_transcript = root / "claude" / "project" / "session.jsonl"
            _write_records(claude_transcript, {"timestamp": now.isoformat()})
            profile_root = root / "profile" / "projects"
            profile_transcript = profile_root / "project" / "session.jsonl"
            _write_records(profile_transcript, {"timestamp": now.isoformat()})
            with (
                mock.patch.object(wd_config, "claude_projects_dir", return_value=root / "claude"),
            ):
                files = wd_activity.activity_files(
                    "claude", (wd_models.ClaudeProfile("work", "Work", profile_root),)
                )
            self.assertEqual(
                [(item.profile_id, item.path) for item in files],
                [(None, claude_transcript), ("work", profile_transcript)],
            )

    def test_aliased_profile_transcript_is_not_a_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            transcript = actual / "project" / "session.jsonl"
            _write_records(transcript, {"timestamp": "2026-08-16T00:00:00Z"})
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with mock.patch.object(wd_config, "claude_projects_dir", return_value=root / "vanilla"):
                files = wd_activity.activity_files(
                    "claude",
                    (
                        wd_models.ClaudeProfile("first", "First", actual),
                        wd_models.ClaudeProfile("second", "Second", alias),
                    ),
                )
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].path, transcript)

    def test_same_canonical_file_remains_distinct_across_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.jsonl"
            _write_records(path, {"timestamp": "2026-08-16T00:00:00Z"})

            def paths(source):
                return [path] if source in {"claude", "codex"} else []

            with mock.patch.object(wd_activity, "_paths_for_source", side_effect=paths):
                files = wd_activity.activity_files("auto")
            self.assertEqual(
                [(item.source, item.path) for item in files],
                [("claude", path), ("codex", path)],
            )

    def test_codex_alias_dedup_preserves_the_discovered_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            transcript = actual / "rollout-session.jsonl"
            _write_records(transcript, {"timestamp": "2026-08-16T00:00:00Z"})
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            discovered = alias / transcript.name
            with mock.patch.object(wd_activity, "_paths_for_source", return_value=[discovered, transcript]
            ):
                files = wd_activity.activity_files("codex")
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].path, discovered)

    def test_missing_profile_root_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(wd_config, "claude_projects_dir", return_value=root / "vanilla"):
                self.assertEqual(
                    wd_activity.activity_files(
                        "claude",
                        (wd_models.ClaudeProfile("work", "Work", root / "missing"),),
                    ),
                    [],
                )

    def test_profile_selection_does_not_adopt_paths_created_after_snapshot(self):
        now = datetime(2026, 8, 16, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "profile" / "projects"
            initial = projects / "project" / "session.jsonl"
            _write_records(
                initial,
                {"timestamp": (now - timedelta(seconds=10)).isoformat()},
            )
            with mock.patch.object(wd_config, "claude_projects_dir", return_value=root / "vanilla"):
                profile = wd_models.ClaudeProfile("work", "Work", projects)
                snapshot = wd_activity.activity_files("claude", (profile,))
                later = projects / "other" / "later.jsonl"
                _write_records(later, {"timestamp": now.isoformat()})
                selected = wd_activity.select_watch_set(
                    wd_models.Config(
                        select_window_seconds=120,
                        source="claude",
                        claude_profiles=(profile,),
                    ),
                    snapshot,
                    now=now,
                )
            self.assertEqual(
                [item.path for item in selected],
                [initial],
            )

    def test_omx_scope_is_global_only(self):
        expected = [Path.home() / ".omx" / "logs"]
        with mock.patch.dict(os.environ, {"OMX_LOG_DIR": "/tmp/unrequested"}):
            self.assertEqual(wd_config.omx_log_dirs(), expected)

    def test_opencode_database_path_honors_xdg_and_official_override(self):
        with mock.patch.dict(
            os.environ,
            {"XDG_DATA_HOME": "", "OPENCODE_DB": ""},
        ):
            self.assertEqual(
                wd_config.opencode_database_path(),
                Path.home() / ".local" / "share" / "opencode" / "opencode.db",
            )

        with tempfile.TemporaryDirectory() as tmp:
            data_home = Path(tmp) / "data"
            absolute = Path(tmp) / "absolute.db"
            with mock.patch.dict(
                os.environ,
                {"XDG_DATA_HOME": str(data_home), "OPENCODE_DB": ""},
            ):
                self.assertEqual(
                    wd_config.opencode_database_path(),
                    data_home / "opencode" / "opencode.db",
                )
                os.environ["OPENCODE_DB"] = "custom.db"
                self.assertEqual(
                    wd_config.opencode_database_path(),
                    data_home / "opencode" / "custom.db",
                )
                os.environ["OPENCODE_DB"] = str(absolute)
                self.assertEqual(wd_config.opencode_database_path(), absolute)
                os.environ["OPENCODE_DB"] = ":memory:"
                self.assertIsNone(wd_config.opencode_database_path())

    def test_launch_window_boundary_and_stale_mtime(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="codex")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boundary = root / "rollout-boundary.jsonl"
            stale = root / "rollout-stale.jsonl"
            _write_records(boundary, {"timestamp": (now - timedelta(seconds=120)).isoformat()})
            _write_records(stale, {"timestamp": (now - timedelta(seconds=121)).isoformat()})
            os.utime(boundary, (1, 1))

            selected = wd_activity.select_watch_set(
                cfg,
                [_item(boundary), _item(stale)],
                now=now,
            )
            self.assertEqual([item.path for item in selected], [boundary])

    def test_selection_does_not_rediscover_paths(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="codex")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "rollout-initial.jsonl"
            later = root / "rollout-later.jsonl"
            _write_records(initial, {"timestamp": (now - timedelta(seconds=10)).isoformat()})
            snapshot = [_item(initial)]
            _write_records(later, {"timestamp": now.isoformat()})

            with mock.patch.object(wd_activity, "activity_files",
                side_effect=AssertionError("post-launch rediscovery"),
            ):
                selected = wd_activity.select_watch_set(cfg, snapshot, now=now)
            self.assertEqual([item.path for item in selected], [initial])

    def test_selection_ignores_content_appended_after_size_snapshot(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="codex")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-existing.jsonl"
            _write_records(path, {"timestamp": (now - timedelta(hours=1)).isoformat()})
            snapshot = _item(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": now.isoformat()}) + "\n")

            self.assertEqual(
                wd_activity.select_watch_set(cfg, [snapshot], now=now),
                [],
            )

    def test_shared_omx_log_only_tracks_launch_identities(self):
        launch = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="omx")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            initial_time = launch - timedelta(seconds=10)
            _write_records(
                path,
                {"timestamp": initial_time.isoformat(), "thread_id": "session-a"},
            )
            selected = wd_activity.select_watch_set(cfg, [_item(path, "omx")], now=launch)
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].identities, frozenset({"session-a"}))

            session_b_time = launch + timedelta(seconds=20)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"timestamp": session_b_time.isoformat(), "thread_id": "session-b"}
                    )
                    + "\n"
                )
            self.assertEqual(
                wd_activity._last_activity_for(selected[0], launch + timedelta(seconds=30)),
                initial_time,
            )

            session_a_time = launch + timedelta(seconds=25)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"timestamp": session_a_time.isoformat(), "thread_id": "session-a"}
                    )
                    + "\n"
                )
            self.assertEqual(
                wd_activity._last_activity_for(selected[0], launch + timedelta(seconds=30)),
                session_a_time,
            )

    def test_omx_launch_scan_skips_stale_out_of_order_record(self):
        launch = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="omx")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            _write_records(
                path,
                {
                    "timestamp": (launch - timedelta(seconds=10)).isoformat(),
                    "thread_id": "session-recent",
                },
                {
                    "timestamp": (launch - timedelta(hours=1)).isoformat(),
                    "thread_id": "session-stale",
                },
            )

            selected = wd_activity.select_watch_set(
                cfg, [_item(path, "omx")], now=launch
            )

            self.assertEqual(len(selected), 1)
            self.assertEqual(
                selected[0].identities,
                frozenset({"session-recent"}),
            )

    def test_shared_omx_read_uses_max_matching_timestamp(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        newer = now - timedelta(seconds=5)
        older = now - timedelta(seconds=20)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            _write_records(
                path,
                {"timestamp": newer.isoformat(), "thread_id": "session-a"},
                {"timestamp": older.isoformat(), "thread_id": "session-a"},
            )

            self.assertEqual(
                wd_activity.last_activity(
                    path,
                    now=now,
                    identities=frozenset({"session-a"}),
                ),
                newer,
            )

    def test_omx_file_field_cannot_link_different_threads(self):
        launch = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="omx")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.jsonl"
            initial = launch - timedelta(seconds=10)
            _write_records(
                path,
                {
                    "timestamp": initial.isoformat(),
                    "thread_id": "session-a",
                    "file": "shared-plugin.jsonl",
                },
            )
            selected = wd_activity.select_watch_set(
                cfg, [_item(path, "omx")], now=launch
            )
            self.assertEqual(selected[0].identities, frozenset({"session-a"}))

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": (launch + timedelta(seconds=20)).isoformat(),
                            "thread_id": "session-b",
                            "file": "shared-plugin.jsonl",
                        }
                    )
                    + "\n"
                )

            self.assertEqual(
                wd_activity._last_activity_for(
                    selected[0], launch + timedelta(seconds=30)
                ),
                initial,
            )

    def test_omx_identity_aliases_are_preserved(self):
        self.assertEqual(
            wd_activity._record_identifiers(
                {
                    "session_id": "session",
                    "native_session_id": "native",
                    "thread_id": "thread",
                    "file": "not-an-identity",
                }
            ),
            frozenset({"session", "native", "thread"}),
        )

    def test_omx_launch_read_permission_error_fails_safe(self):
        item = wd_models.ActivityFile(
            Path("denied.jsonl"), "omx", snapshot_size=1
        )
        with mock.patch.object(wd_activity, "_json_objects_reverse",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaisesRegex(wd_models.ActivityReadError, "denied"):
                wd_activity._freeze_omx_item(
                    item,
                    wd_models.Config(source="omx"),
                    datetime(2026, 8, 14, tzinfo=timezone.utc),
                )

    def test_shared_opencode_database_only_tracks_launch_identities(self):
        launch = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="opencode")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            database = _open_opencode_fixture(path)
            try:
                initial_time = launch - timedelta(seconds=10)
                database.executemany(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    [
                        ("session-a", _epoch_millis(initial_time)),
                        ("session-stale", _epoch_millis(launch - timedelta(hours=1))),
                    ],
                )
                database.commit()
                os.utime(path, (1, 1))

                selected = wd_activity.select_watch_set(
                    cfg,
                    [_item(path, "opencode")],
                    now=launch,
                )
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0].identities, frozenset({"session-a"}))

                database.execute(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    ("session-b", _epoch_millis(launch + timedelta(seconds=20))),
                )
                database.commit()
                self.assertEqual(
                    wd_activity._last_activity_for(
                        selected[0], launch + timedelta(seconds=30)
                    ),
                    initial_time,
                )

                session_a_time = launch + timedelta(seconds=25)
                database.execute(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    ("session-a", _epoch_millis(session_a_time)),
                )
                database.commit()
                self.assertEqual(
                    wd_activity._last_activity_for(
                        selected[0], launch + timedelta(seconds=30)
                    ),
                    session_a_time,
                )
            finally:
                database.close()

    def test_opencode_tracks_post_launch_descendants_but_not_new_roots(self):
        launch = datetime(2026, 8, 15, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="opencode")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            database = _open_opencode_fixture(path)
            try:
                root_time = launch - timedelta(seconds=10)
                database.execute(
                    "INSERT INTO session (id, parent_id) VALUES (?, ?)",
                    ("root-session", None),
                )
                database.execute(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    ("root-session", _epoch_millis(root_time)),
                )
                database.commit()

                selected = wd_activity.select_watch_set(
                    cfg,
                    [_item(path, "opencode")],
                    now=launch,
                )
                self.assertEqual(len(selected), 1)
                self.assertEqual(
                    selected[0].identities,
                    frozenset({"root-session"}),
                )

                child_time = launch + timedelta(seconds=20)
                grandchild_time = launch + timedelta(seconds=25)
                unrelated_time = launch + timedelta(seconds=30)
                database.executemany(
                    "INSERT INTO session (id, parent_id) VALUES (?, ?)",
                    [
                        ("child-session", "root-session"),
                        ("grandchild-session", "child-session"),
                        ("unrelated-session", None),
                    ],
                )
                database.executemany(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    [
                        ("child-session", _epoch_millis(child_time)),
                        ("grandchild-session", _epoch_millis(grandchild_time)),
                        ("unrelated-session", _epoch_millis(unrelated_time)),
                    ],
                )
                database.commit()

                self.assertEqual(
                    wd_activity._last_activity_for(
                        selected[0], launch + timedelta(seconds=35)
                    ),
                    grandchild_time,
                )
            finally:
                database.close()

    def test_opencode_future_timestamp_does_not_mask_recent_activity(self):
        launch = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cfg = wd_models.Config(select_window_seconds=120, source="opencode")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            database = _open_opencode_fixture(path)
            try:
                recent = launch - timedelta(seconds=10)
                database.execute(
                    "INSERT INTO message (session_id, time_updated) VALUES (?, ?)",
                    ("session-a", _epoch_millis(recent)),
                )
                database.executemany(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    [
                        ("session-a", _epoch_millis(launch + timedelta(days=30))),
                        ("session-future", _epoch_millis(launch + timedelta(days=30))),
                    ],
                )
                database.commit()

                selected = wd_activity.select_watch_set(
                    cfg,
                    [_item(path, "opencode")],
                    now=launch,
                )

                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0].identities, frozenset({"session-a"}))
                self.assertEqual(
                    wd_activity._last_activity_for(selected[0], launch),
                    recent,
                )
            finally:
                database.close()

    def test_unattributed_omx_log_is_not_selected(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "omx.jsonl"
            _write_records(path, {"timestamp": now.isoformat(), "event": "background"})
            selected = wd_activity.select_watch_set(
                wd_models.Config(source="omx"),
                [_item(path, "omx")],
                now=now,
            )
            self.assertEqual(selected, [])


class LiveSessionAdmissionTests(unittest.TestCase):
    def test_refresh_adds_new_recent_jsonl_path_once_and_is_pure(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        cfg = wd_models.Config(source="codex", select_window_seconds=120)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_path = root / "rollout-first.jsonl"
            second_path = root / "rollout-second.jsonl"
            _write_records(first_path, {"timestamp": now.isoformat()})
            _write_records(second_path, {"timestamp": now.isoformat()})
            first = _item(first_path)
            second = _item(second_path)
            current = [first]

            refreshed = wd_activity.refresh_watch_set(
                cfg, current, candidates=[first, second], now=now
            )
            repeated = wd_activity.refresh_watch_set(
                cfg, refreshed, candidates=[first, second], now=now
            )

            self.assertIsNot(refreshed, current)
            self.assertEqual(current, [first])
            self.assertEqual(refreshed, [first, second])
            self.assertEqual(repeated, [first, second])

    def test_refresh_filters_injected_candidates_to_configured_source(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_path = root / "rollout-codex.jsonl"
            claude_path = root / "claude.jsonl"
            _write_records(codex_path, {"timestamp": now.isoformat()})
            _write_records(claude_path, {"timestamp": now.isoformat()})
            refreshed = wd_activity.refresh_watch_set(
                wd_models.Config(source="codex", select_window_seconds=120),
                [],
                candidates=[
                    _item(claude_path, "claude"),
                    _item(codex_path, "codex"),
                ],
                now=now,
            )
            self.assertEqual(
                [(item.source, item.path) for item in refreshed],
                [("codex", codex_path)],
            )

    def test_refresh_retains_missing_target_and_uses_scan_size_boundary(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        cfg = wd_models.Config(source="codex", select_window_seconds=120)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retained_path = root / "rollout-retained.jsonl"
            candidate_path = root / "rollout-candidate.jsonl"
            _write_records(retained_path, {"timestamp": now.isoformat()})
            _write_records(
                candidate_path,
                {"timestamp": (now - timedelta(hours=1)).isoformat()},
            )
            retained = _item(retained_path)
            stale_snapshot = _item(candidate_path)
            with candidate_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": now.isoformat()}) + "\n")

            first_refresh = wd_activity.refresh_watch_set(
                cfg, [retained], candidates=[stale_snapshot], now=now
            )
            second_refresh = wd_activity.refresh_watch_set(
                cfg, first_refresh, candidates=[_item(candidate_path)], now=now
            )

            self.assertEqual(first_refresh, [retained])
            self.assertEqual(
                [item.path for item in second_refresh],
                [retained_path, candidate_path],
            )

    def test_refresh_merges_omx_identities_and_opencode_seeds(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            omx_path = root / "turns.jsonl"
            _write_records(
                omx_path,
                {"timestamp": now.isoformat(), "thread_id": "session-a"},
                {"timestamp": now.isoformat(), "native_session_id": "session-b"},
                {"timestamp": now.isoformat(), "event": "unattributed"},
            )
            current_omx = wd_models.ActivityFile(
                omx_path, "omx", identities=frozenset({"session-a"})
            )
            merged_omx = wd_activity.refresh_watch_set(
                wd_models.Config(source="omx", select_window_seconds=120),
                [current_omx],
                candidates=[_item(omx_path, "omx")],
                now=now,
            )
            self.assertEqual(
                merged_omx[0].identities,
                frozenset({"session-a", "session-b"}),
            )

            database_path = root / "opencode.db"
            database = _open_opencode_fixture(database_path)
            try:
                database.executemany(
                    "INSERT INTO session (id, parent_id) VALUES (?, ?)",
                    [("seed-a", None), ("seed-b", None), ("stale", None)],
                )
                database.executemany(
                    "INSERT INTO part (session_id, time_updated) VALUES (?, ?)",
                    [
                        ("seed-a", _epoch_millis(now)),
                        ("seed-b", _epoch_millis(now)),
                        ("stale", _epoch_millis(now - timedelta(hours=1))),
                    ],
                )
                database.commit()
                current_db = wd_models.ActivityFile(
                    database_path,
                    "opencode",
                    identities=frozenset({"seed-a"}),
                )
                merged_db = wd_activity.refresh_watch_set(
                    wd_models.Config(source="opencode", select_window_seconds=120),
                    [current_db],
                    candidates=[_item(database_path, "opencode")],
                    now=now,
                )
                self.assertEqual(
                    merged_db[0].identities,
                    frozenset({"seed-a", "seed-b"}),
                )
            finally:
                database.close()

    def test_live_refresh_precedes_activity_and_logs_new_target_once(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        old = wd_models.ActivityFile(Path("old.jsonl"), "codex")
        new = wd_models.ActivityFile(Path("new.jsonl"), "codex")
        cfg = wd_models.Config(
            source="codex",
            session_discovery="live",
            idle_minutes=1,
            user_idle_minutes=0,
            poll_seconds=0.01,
        )
        order = []

        def discover(source):
            order.append("discover")
            return [new]

        def select(config, candidates=None, now=None):
            order.append("select")
            self.assertEqual(list(candidates), [new])
            return [new]

        def activity(item, current_time):
            order.append(f"activity:{item.path.name}")
            return current_time if item is new else current_time - timedelta(hours=1)

        with (
            mock.patch.object(wd_activity, "activity_files", side_effect=discover),
            mock.patch.object(wd_activity, "select_watch_set", side_effect=select),
            mock.patch.object(wd_activity, "_last_activity_for", side_effect=activity),
            mock.patch.object(wd_app.time, "sleep", side_effect=RuntimeError("polled")),
            self.assertLogs(wd_models.log, level="INFO") as captured,
        ):
            with self.assertRaisesRegex(RuntimeError, "polled"):
                wd_app.wait_until_quiet(cfg, [old])

        self.assertEqual(order[:3], ["discover", "select", "activity:old.jsonl"])
        self.assertEqual(
            sum(
                "admitted new activity target: codex:new.jsonl" in line
                for line in captured.output
            ),
            1,
        )

    def test_frozen_mode_never_rediscovers(self):
        cfg = wd_models.Config(
            source="codex",
            session_discovery="frozen",
            idle_minutes=1,
            user_idle_minutes=0,
        )
        item = wd_models.ActivityFile(Path("old.jsonl"), "codex")
        with (
            mock.patch.object(wd_activity, "activity_files", side_effect=AssertionError("rediscovered")
            ),
            mock.patch.object(wd_activity, "refresh_watch_set", side_effect=AssertionError("refreshed")
            ),
            mock.patch.object(wd_activity, "_last_activity_for",
                return_value=datetime.now(timezone.utc) - timedelta(hours=1),
            ),
        ):
            wd_app.wait_until_quiet(cfg, [item])

    def test_codex_traversal_is_recursive_but_skips_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "sessions"
            nested = root / "year" / "month"
            nested.mkdir(parents=True)
            expected = nested / "rollout-real.jsonl"
            ignored = nested / "other.jsonl"
            _write_records(expected, {"timestamp": "2026-09-05T00:00:00Z"})
            _write_records(ignored, {"timestamp": "2026-09-05T00:00:00Z"})
            external = base / "external"
            external.mkdir()
            _write_records(
                external / "rollout-through-link.jsonl",
                {"timestamp": "2026-09-05T00:00:00Z"},
            )
            (root / "linked").symlink_to(external, target_is_directory=True)
            with mock.patch.object(wd_config, "codex_sessions_dir", return_value=root):
                paths = wd_activity._paths_for_source("codex")
            self.assertEqual(paths, [expected])

    def test_missing_roots_are_absent_but_resolution_errors_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with mock.patch.object(wd_config, "codex_sessions_dir", return_value=missing):
                self.assertEqual(wd_activity.activity_files("codex"), [])
            with mock.patch.object(wd_config, "opencode_database_path", return_value=missing / "opencode.db"
            ):
                self.assertEqual(wd_activity.activity_files("opencode"), [])

    def test_opencode_stat_error_is_explicit(self):
        path = Path("/denied/opencode.db")
        with (
            mock.patch.object(wd_config, "opencode_database_path", return_value=path),
            mock.patch.object(Path, "stat", side_effect=PermissionError("denied")),
        ):
            with self.assertRaisesRegex(wd_models.ActivityReadError, "OpenCode.*denied"):
                wd_activity.activity_files("opencode")

    def test_provider_root_permission_error_is_explicit(self):
        root = Path("/denied/codex")
        with (
            mock.patch.object(wd_config, "codex_sessions_dir", return_value=root),
            mock.patch.object(
                wd_activity.os, "scandir", side_effect=PermissionError("denied")
            ),
        ):
            with self.assertRaisesRegex(wd_models.ActivityReadError, "codex.*denied"):
                wd_activity.activity_files("codex")


class QuietnessAndPowerTests(unittest.TestCase):
    def test_block_sleep_is_bound_to_watchdog_process(self):
        process = mock.Mock()
        with mock.patch.object(
            wd_power.subprocess, "Popen", return_value=process
        ) as popen:
            self.assertIs(wd_power.block_sleep(), process)
        popen.assert_called_once_with(
            ["caffeinate", "-is", "-w", str(os.getpid())]
        )

    def test_caffeinate_timeout_is_killed_and_reaped(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["caffeinate"], 5),
            0,
        ]

        wd_power._stop_caffeinate(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_opencode_status_names_database_activity_signal(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        item = wd_models.ActivityFile(
            Path("opencode.db"),
            "opencode",
            identities=frozenset({"session-a"}),
        )

        status = wd_app._source_guard_status(
            now,
            30 * 60,
            [item],
            [(item, now - timedelta(seconds=5))],
        )

        self.assertEqual(
            status,
            "opencode=holding (last database activity 5s ago; 1 database)",
        )

    def test_profile_status_remains_part_of_the_claude_jsonl_guard(self):
        now = datetime(2026, 8, 16, tzinfo=timezone.utc)
        item = wd_models.ActivityFile(
            Path("work.jsonl"), "claude", profile_id="work", profile_label="Work"
        )
        status = wd_app._source_guard_status(
            now,
            30 * 60,
            [item],
            [(item, now - timedelta(seconds=2))],
        )
        self.assertEqual(
            status,
            "claude=holding (last JSONL event 2s ago; 1 file)",
        )

    def test_source_status_reports_each_provider_as_a_guard_not_liveness(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        claude_items = [
            wd_models.ActivityFile(Path(f"claude-{index}.jsonl"), "claude")
            for index in range(3)
        ]
        codex_items = [
            wd_models.ActivityFile(Path(f"codex-{index}.jsonl"), "codex")
            for index in range(3)
        ]
        status = wd_app._source_guard_status(
            now,
            30 * 60,
            claude_items + codex_items,
            [
                (claude_items[0], now - timedelta(seconds=405)),
                (codex_items[0], now - timedelta(seconds=1)),
            ],
        )

        self.assertEqual(
            status,
            "claude=holding (last JSONL event 405s ago; 3 files), "
            "codex=holding (last JSONL event 1s ago; 3 files)",
        )

    def test_source_status_marks_quiet_and_unavailable_providers(self):
        now = datetime(2026, 8, 14, tzinfo=timezone.utc)
        claude = wd_models.ActivityFile(Path("claude.jsonl"), "claude")
        codex = wd_models.ActivityFile(Path("codex.jsonl"), "codex")

        status = wd_app._source_guard_status(
            now,
            30 * 60,
            [claude, codex],
            [(claude, now - timedelta(seconds=1800))],
        )

        self.assertEqual(
            status,
            "claude=quiet (last JSONL event 1800s ago; 1 file), "
            "codex=quiet (last JSONL event unavailable; 1 file)",
        )

    def test_gone_file_is_quiet_and_disabled_user_gate_skips_ioreg(self):
        missing = wd_models.ActivityFile(Path("/definitely/missing.jsonl"), "codex")
        cfg = wd_models.Config(
            user_idle_minutes=0, poll_seconds=0.01, session_discovery="frozen"
        )
        with mock.patch.object(wd_power, "user_idle_seconds",
            side_effect=AssertionError("ioreg should be skipped"),
        ):
            wd_app.wait_until_quiet(cfg, [missing])

    def test_freshest_source_must_be_quiet(self):
        cfg = wd_models.Config(
            idle_minutes=1,
            user_idle_minutes=0,
            poll_seconds=0.01,
            session_discovery="frozen",
        )
        items = [
            wd_models.ActivityFile(Path("old.jsonl"), "claude"),
            wd_models.ActivityFile(Path("recent.jsonl"), "codex"),
        ]

        def activity(item, now):
            if item.path.name == "old.jsonl":
                return now - timedelta(minutes=10)
            return now - timedelta(seconds=5)

        with (
            mock.patch.object(wd_activity, "_last_activity_for", side_effect=activity),
            mock.patch.object(wd_app.time, "sleep", side_effect=RuntimeError("polled")),
        ):
            with self.assertRaisesRegex(RuntimeError, "polled"):
                wd_app.wait_until_quiet(cfg, items)

    def test_active_session_defers_ioreg_query(self):
        cfg = wd_models.Config(
            idle_minutes=1,
            user_idle_minutes=5,
            poll_seconds=0.01,
            session_discovery="frozen",
        )
        item = wd_models.ActivityFile(Path("recent.jsonl"), "codex")
        with (
            mock.patch.object(wd_activity, "_last_activity_for",
                side_effect=lambda item, now: now - timedelta(seconds=5),
            ),
            mock.patch.object(wd_power, "user_idle_seconds",
                side_effect=AssertionError("ioreg queried while session active"),
            ),
            mock.patch.object(
                wd_app.time,
                "sleep",
                side_effect=RuntimeError("polled"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "polled"):
                wd_app.wait_until_quiet(cfg, [item])

    def test_ioreg_failure_is_explicit(self):
        with mock.patch.object(
            wd_power.subprocess,
            "check_output",
            side_effect=OSError("ioreg unavailable"),
        ):
            with self.assertRaises(wd_models.PresenceCheckError):
                wd_power.user_idle_seconds()

    def test_dry_run_never_invokes_pmset(self):
        with mock.patch.object(wd_power.subprocess, "run") as run:
            wd_power.force_sleep(True)
        run.assert_not_called()

    def test_pmset_failure_is_reported(self):
        failure = subprocess.CalledProcessError(
            1,
            ["pmset", "sleepnow"],
            stderr="not permitted",
        )
        with mock.patch.object(wd_power.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(wd_models.PowerCommandError, "not permitted"):
                wd_power.force_sleep(False)


class CliAndLifecycleTests(unittest.TestCase):
    def test_session_discovery_cli_defaults_to_live_and_accepts_frozen(self):
        self.assertEqual(wd_config.parse_args([]).session_discovery, "live")
        self.assertEqual(
            wd_config.parse_args(["--session-discovery", "frozen"]).session_discovery,
            "frozen",
        )
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                wd_config.parse_args(["--session-discovery", "unknown"])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_compatibility_and_source_option(self):
        cfg = wd_config.parse_args(["12", "--source", "codex-omx", "--dry-run"])
        self.assertEqual(cfg.idle_minutes, 12)
        self.assertEqual(cfg.source, "codex-omx")
        self.assertTrue(cfg.dry_run)
        self.assertEqual(wd_config.parse_args([]).source, "auto")
        self.assertEqual(
            wd_config.parse_args(["--source", "opencode"]).source,
            "opencode",
        )
        with mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                wd_config.parse_args(["--source", "private-wrapper"])
        self.assertEqual(raised.exception.code, 2)

    def test_profiles_file_cli_loads_once_and_explicit_missing_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "claude_profiles": [
                            {
                                "id": "work",
                                "label": "Work",
                                "projects_dir": str(root / "projects"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cfg = wd_config.parse_args(["--profiles-file", str(path)])
            self.assertEqual(cfg.profiles_file, path.resolve())
            self.assertEqual(cfg.claude_profiles[0].id, "work")

            with self.assertRaises(wd_models.ActivityReadError):
                wd_config.parse_args(["--profiles-file", str(root / "missing.json")])

    def test_non_claude_source_ignores_default_profiles_but_rejects_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            malformed = Path(tmp) / "profiles.json"
            malformed.write_text("{", encoding="utf-8")
            with mock.patch.object(wd_config, "claude_profiles_path", return_value=malformed
            ):
                cfg = wd_config.parse_args(["--source", "codex"])
            self.assertEqual(cfg.claude_profiles, ())

            with mock.patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    wd_config.parse_args(
                        ["--source", "codex", "--profiles-file", str(malformed)]
                    )
            self.assertEqual(raised.exception.code, 2)

    def test_invalid_profile_config_prevents_caffeinate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            path.write_text("{", encoding="utf-8")
            stderr = io.StringIO()
            with (
                mock.patch.object(wd_power, "block_sleep") as block_sleep,
                mock.patch("sys.stderr", new=stderr),
            ):
                self.assertEqual(
                    wd_app.main(["--profiles-file", str(path)]), 1
                )
            block_sleep.assert_not_called()
            self.assertIn("not valid JSON", stderr.getvalue())

    def test_cli_rejects_invalid_timing_values(self):
        invalid = (
            ["0"],
            ["-1"],
            ["--poll", "0"],
            ["--poll", "nan"],
            ["--select-window", "-1"],
            ["--user-idle-minutes", "-1"],
        )
        for argv in invalid:
            with self.subTest(argv=argv), mock.patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    wd_config.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_no_active_launch_snapshot_exits_without_caffeinate(self):
        cfg = wd_models.Config(source="codex")
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[]),
            mock.patch.object(wd_power, "block_sleep") as block_sleep,
        ):
            self.assertEqual(wd_app.main([]), 1)
        block_sleep.assert_not_called()

    def test_opencode_selection_error_exits_without_caffeinate(self):
        cfg = wd_models.Config(source="opencode")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            sqlite3.connect(path).close()
            item = _item(path, "opencode")
            with (
                mock.patch.object(wd_config, "parse_args", return_value=cfg),
                mock.patch.object(wd_app, "setup_logging"),
                mock.patch.object(wd_activity, "activity_files", return_value=[item]),
                mock.patch.object(wd_power, "block_sleep") as block_sleep,
            ):
                self.assertEqual(wd_app.main([]), 1)
            block_sleep.assert_not_called()

    def test_opencode_lineage_schema_is_validated_before_caffeinate(self):
        launch = datetime.now(timezone.utc)
        cfg = wd_models.Config(source="opencode")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            database = sqlite3.connect(path)
            database.execute(
                "CREATE TABLE message (session_id TEXT, time_updated INTEGER)"
            )
            database.execute(
                "CREATE TABLE part (session_id TEXT, time_updated INTEGER)"
            )
            database.execute(
                "INSERT INTO message (session_id, time_updated) VALUES (?, ?)",
                ("session-a", _epoch_millis(launch)),
            )
            database.commit()
            database.close()
            item = _item(path, "opencode")

            with (
                mock.patch.object(wd_config, "parse_args", return_value=cfg),
                mock.patch.object(wd_app, "setup_logging"),
                mock.patch.object(wd_activity, "activity_files", return_value=[item]),
                mock.patch.object(wd_power, "block_sleep") as block_sleep,
            ):
                self.assertEqual(wd_app.main([]), 1)
            block_sleep.assert_not_called()

    def test_successful_dry_run_releases_caffeinate_then_skips_sleep(self):
        cfg = wd_models.Config(source="codex", dry_run=True, display="log")
        item = wd_models.ActivityFile(Path("rollout.jsonl"), "codex")
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_power, "block_sleep", return_value=process),
            mock.patch.object(wd_app, "wait_until_quiet") as wait_until_quiet,
            mock.patch.object(wd_power, "force_sleep") as force_sleep,
        ):
            self.assertEqual(wd_app.main([]), 0)

        wait_until_quiet.assert_called_once_with(cfg, [item])
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        force_sleep.assert_called_once_with(True)

    def test_interrupt_releases_caffeinate_without_sleep(self):
        cfg = wd_models.Config(source="codex", dry_run=True, display="log")
        item = wd_models.ActivityFile(Path("rollout.jsonl"), "codex")
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_power, "block_sleep", return_value=process),
            mock.patch.object(wd_app, "wait_until_quiet", side_effect=KeyboardInterrupt),
            mock.patch.object(wd_power, "force_sleep") as force_sleep,
        ):
            self.assertEqual(wd_app.main([]), 130)
        process.terminate.assert_called_once()
        force_sleep.assert_not_called()

    def test_presence_error_releases_caffeinate_without_sleep(self):
        cfg = wd_models.Config(source="codex", display="log")
        item = wd_models.ActivityFile(Path("rollout.jsonl"), "codex")
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_power, "block_sleep", return_value=process),
            mock.patch.object(wd_app, "wait_until_quiet",
                side_effect=wd_models.PresenceCheckError("unavailable"),
            ),
            mock.patch.object(wd_power, "force_sleep") as force_sleep,
        ):
            self.assertEqual(wd_app.main([]), 1)
        process.terminate.assert_called_once()
        force_sleep.assert_not_called()

    def test_refresh_error_releases_caffeinate_without_sleep(self):
        cfg = wd_models.Config(source="codex", session_discovery="live", display="log")
        item = wd_models.ActivityFile(Path("rollout.jsonl"), "codex")
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files",
                side_effect=([item], wd_models.ActivityReadError("refresh denied")),
            ),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_power, "block_sleep", return_value=process),
            mock.patch.object(wd_power, "force_sleep") as force_sleep,
        ):
            self.assertEqual(wd_app.main([]), 1)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        force_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
