#!/usr/bin/env python3
"""Regression tests for process-confirmed OMX-to-Claude display lineage."""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from claude_watchdog import config as wd_config
from claude_watchdog import dashboard as wd_dashboard
from claude_watchdog import metadata as wd_metadata
from claude_watchdog import models as wd_models
from claude_watchdog import process_lineage as wd_process_lineage


OBSERVED_AT = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
LOCAL_TIMEZONE = timezone(timedelta(hours=3))


def _metadata() -> dict[tuple[str, str], wd_models.SessionMetadata]:
    return {
        ("claude", "/synthetic/claude.jsonl"): wd_models.SessionMetadata(
            session_id="claude-child"
        ),
        ("codex", "/synthetic/codex.jsonl"): wd_models.SessionMetadata(
            session_id="codex-orchestrator"
        ),
    }


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["/bin/ps", "-axo", "pid=,ppid=,lstart="], 0, stdout, ""
    )


def _write_provider_evidence(root: Path) -> tuple[Path, Path, Path]:
    registry = root / "sessions"
    registry.mkdir()
    project = root / "project"
    state = project / ".omx" / "state"
    state.mkdir(parents=True)
    registry_file = registry / "33394.json"
    registry_file.write_text(
        json.dumps(
            {
                "pid": 33394,
                "sessionId": "claude-child",
                "cwd": str(project),
                "procStart": "Thu Sep 10 17:05:34 2026",
            }
        ),
        encoding="utf-8",
    )
    (state / "session.json").write_text(
        json.dumps(
            {
                "pid": 33393,
                "native_session_id": "codex-orchestrator",
                "started_at": "2026-08-01T14:17:52.000Z",
            }
        ),
        encoding="utf-8",
    )
    return registry, registry_file, project


def _valid_runner(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    return _completed(
        "33394 33393 Thu Sep 10 20:05:34 2026\n"
        "33393 1 Sat Aug  1 17:17:22 2026\n"
    )


def _discover(
    root: Path,
    stdout: str,
    *,
    metadata: dict[tuple[str, str], wd_models.SessionMetadata] | None = None,
):
    registry, _, _ = _write_provider_evidence(root)
    return wd_process_lineage.discover_process_lineage(
        metadata or _metadata(),
        registry,
        runner=lambda *args, **kwargs: _completed(stdout),
        observed_at=OBSERVED_AT,
        local_timezone=LOCAL_TIMEZONE,
    )


def _write_transcripts(root: Path, project: Path):
    claude_path = root / "claude.jsonl"
    claude_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "claude-child",
                "cwd": str(project),
                "timestamp": "2026-09-10T17:05:35Z",
                "message": {"model": "synthetic-model"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    codex_path = root / "codex.jsonl"
    codex_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-08-01T14:17:52Z",
                "payload": {
                    "id": "codex-orchestrator",
                    "cwd": str(project),
                    "source": "cli",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return (
        wd_models.ActivityFile(claude_path, "claude"),
        wd_models.ActivityFile(codex_path, "codex"),
    )


class ProcessConfirmedLineageTests(unittest.TestCase):
    def test_confirms_exact_registry_and_omx_records_across_utc_and_local_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, _ = _write_provider_evidence(root)
            calls: list[tuple[object, ...]] = []

            def runner(*args, **kwargs):
                calls.append((args, kwargs))
                return _completed(
                    "33394 33393 Thu Sep 10 20:05:34 2026\n"
                    "33393 1 Sat Aug  1 17:17:22 2026\n"
                )

            links = wd_process_lineage.discover_process_lineage(
                _metadata(),
                registry,
                runner=runner,
                observed_at=OBSERVED_AT,
                local_timezone=LOCAL_TIMEZONE,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            (
                (["/bin/ps", "-axo", "pid=,ppid=,lstart="],),
                {"capture_output": True, "text": True, "timeout": 1.0, "check": False},
            ),
        )
        self.assertEqual(
            links[0]["child"], {"source": "claude", "session_id": "claude-child"}
        )
        self.assertEqual(
            links[0]["parent"],
            {"source": "codex", "session_id": "codex-orchestrator"},
        )
        self.assertIn("Claude pid 33394", links[0]["evidence"])
        self.assertIn("OMX pid 33393", links[0]["evidence"])
        self.assertIn("2026-09-10T18:00:00+00:00", links[0]["evidence"])
        self.assertIn("shallower", links[0]["evidence"])

    def test_multiple_candidates_share_one_process_table_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            second = json.loads(registry_file.read_text(encoding="utf-8"))
            second.update({"pid": 33395, "sessionId": "claude-second"})
            (registry / "33395.json").write_text(json.dumps(second), encoding="utf-8")
            metadata = _metadata()
            metadata[("claude", "/synthetic/second.jsonl")] = (
                wd_models.SessionMetadata(session_id="claude-second")
            )
            calls = 0

            def runner(*args, **kwargs):
                nonlocal calls
                calls += 1
                return _completed(
                    "33394 33393 Thu Sep 10 20:05:34 2026\n"
                    "33395 33393 Thu Sep 10 20:05:34 2026\n"
                    "33393 1 Sat Aug  1 17:17:22 2026\n"
                )

            links = wd_process_lineage.discover_process_lineage(
                metadata,
                registry,
                runner=runner,
                observed_at=OBSERVED_AT,
                local_timezone=LOCAL_TIMEZONE,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(
            {link["child"]["session_id"] for link in links},
            {"claude-child", "claude-second"},
        )

    def test_confirmed_link_is_retained_after_claude_registry_file_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, project = _write_provider_evidence(root)
            child, parent = _write_transcripts(root, project)
            retained: list[dict[str, object]] = []
            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(
                    wd_metadata, "external_lineage_registry_path", return_value=root / "missing.json"
                ),
                mock.patch.object(wd_metadata, "codex_sqlite_metadata_batch", return_value={}),
            ):
                first = wd_metadata.load_dashboard_metadata(
                    [child, parent],
                    injected_links=retained,
                    process_runner=_valid_runner,
                    process_observed_at=OBSERVED_AT,
                    process_local_timezone=LOCAL_TIMEZONE,
                )
                registry_file.unlink()
                second = wd_metadata.load_dashboard_metadata(
                    [child, parent],
                    injected_links=retained,
                    process_runner=lambda *args, **kwargs: _completed(""),
                    process_observed_at=OBSERVED_AT + timedelta(minutes=1),
                    process_local_timezone=LOCAL_TIMEZONE,
                )

        child_key = wd_metadata.target_key(child)
        parent_key = wd_metadata.target_key(parent)
        self.assertEqual(len(retained), 1)
        self.assertEqual(first[child_key].external_parent_key, parent_key)
        self.assertEqual(second[child_key].external_parent_key, parent_key)
        self.assertEqual(second[child_key].details, first[child_key].details)

    def test_never_confirmed_link_never_appears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            child, parent = _write_transcripts(root, project)
            retained: list[dict[str, object]] = []
            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(
                    wd_metadata, "external_lineage_registry_path", return_value=root / "missing.json"
                ),
                mock.patch.object(wd_metadata, "codex_sqlite_metadata_batch", return_value={}),
            ):
                result = wd_metadata.load_dashboard_metadata(
                    [child, parent],
                    injected_links=retained,
                    process_runner=lambda *args, **kwargs: _completed(""),
                    process_observed_at=OBSERVED_AT,
                    process_local_timezone=LOCAL_TIMEZONE,
                )

        self.assertEqual(retained, [])
        self.assertIsNone(result[wd_metadata.target_key(child)].external_parent_key)

    def test_confirmed_link_does_not_draw_when_parent_row_is_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            child, _ = _write_transcripts(root, project)
            retained: list[dict[str, object]] = []
            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(
                    wd_metadata, "external_lineage_registry_path", return_value=root / "missing.json"
                ),
            ):
                result = wd_metadata.load_dashboard_metadata(
                    [child],
                    injected_links=retained,
                    process_runner=_valid_runner,
                    process_observed_at=OBSERVED_AT,
                    process_local_timezone=LOCAL_TIMEZONE,
                )

        self.assertEqual(len(retained), 1)
        self.assertIsNone(result[wd_metadata.target_key(child)].external_parent_key)

    def test_unrendered_confirmation_is_retained_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            child, _ = _write_transcripts(root, project)
            retained: list[dict[str, object]] = []
            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(
                    wd_metadata, "external_lineage_registry_path", return_value=root / "missing.json"
                ),
            ):
                for _ in range(2):
                    wd_metadata.load_dashboard_metadata(
                        [child],
                        injected_links=retained,
                        process_runner=_valid_runner,
                        process_observed_at=OBSERVED_AT,
                        process_local_timezone=LOCAL_TIMEZONE,
                    )

        self.assertEqual(len(retained), 1)

    def test_interactive_claude_in_omx_cwd_is_refused_when_omx_pid_is_not_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary),
                "33394 30000 Thu Sep 10 20:05:34 2026\n"
                "30000 1 Thu Sep 10 20:05:30 2026\n"
                "33393 1 Sat Aug  1 17:17:22 2026\n",
            )
        self.assertEqual(links, ())

    def test_recycled_claude_pid_is_refused_by_exact_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary),
                "33394 33393 Thu Sep 10 20:05:35 2026\n"
                "33393 1 Sat Aug  1 17:17:22 2026\n",
            )
        self.assertEqual(links, ())

    def test_recycled_omx_pid_outside_start_tolerance_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary),
                "33394 33393 Thu Sep 10 20:05:34 2026\n"
                "33393 1 Sat Aug  1 17:15:51 2026\n",
            )
        self.assertEqual(links, ())

    def test_registry_file_vanishing_between_scan_and_read_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            original_open = Path.open

            def disappearing_open(path, *args, **kwargs):
                if path == registry_file:
                    registry_file.unlink()
                    raise FileNotFoundError(path)
                return original_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", disappearing_open):
                links = wd_process_lineage.discover_process_lineage(
                    _metadata(),
                    registry,
                    runner=_valid_runner,
                    observed_at=OBSERVED_AT,
                    local_timezone=LOCAL_TIMEZONE,
                )
        self.assertEqual(links, ())

    def test_missing_omx_session_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            (project / ".omx" / "state" / "session.json").unlink()
            calls = 0

            def forbidden_runner(*args, **kwargs):
                nonlocal calls
                calls += 1
                return _valid_runner(*args, **kwargs)

            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=forbidden_runner
            )
        self.assertEqual(links, ())
        self.assertEqual(calls, 0)

    def test_oversized_omx_session_file_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            path = project / ".omx" / "state" / "session.json"
            path.write_bytes(
                path.read_bytes()
                + b" " * (wd_models.MAX_OMX_SESSION_BYTES + 1)
            )
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_oversized_omx_session_file_is_not_launch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            state = project / ".omx" / "state"
            state.mkdir(parents=True)
            document = json.dumps(
                {
                    "session_id": "omx-synthetic-launch",
                    "native_session_id": "codex-orchestrator",
                }
            ).encode("utf-8")
            (state / "session.json").write_bytes(
                document
                + b" " * (wd_models.MAX_OMX_SESSION_BYTES + 1)
            )
            transcript = root / "codex.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-08-01T14:17:52Z",
                        "payload": {
                            "id": "codex-orchestrator",
                            "cwd": str(project),
                            "source": "cli",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = wd_metadata.jsonl_metadata(
                wd_models.ActivityFile(transcript, "codex")
            )

        self.assertNotEqual(result.client, "OMX / Codex")

    def test_non_integer_omx_session_pid_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            path = project / ".omx" / "state" / "session.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pid"] = "33393"
            path.write_text(json.dumps(document), encoding="utf-8")
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_boolean_omx_session_pid_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            path = project / ".omx" / "state" / "session.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pid"] = True
            path.write_text(json.dumps(document), encoding="utf-8")

            def runner(*args, **kwargs):
                return _completed(
                    "33394 1 Thu Sep 10 20:05:34 2026\n"
                    "1 0 Sat Aug  1 17:17:22 2026\n"
                )

            links = wd_process_lineage.discover_process_lineage(
                _metadata(),
                registry,
                runner=runner,
                observed_at=OBSERVED_AT,
                local_timezone=LOCAL_TIMEZONE,
            )
        self.assertEqual(links, ())

    def test_non_string_omx_native_session_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            path = project / ".omx" / "state" / "session.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["native_session_id"] = 12345
            path.write_text(json.dumps(document), encoding="utf-8")
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_ancestry_chain_exceeding_hop_bound_is_refused(self) -> None:
        first_parent = 40000
        rows = ["33394 40000 Thu Sep 10 20:05:34 2026"]
        for offset in range(64):
            pid = first_parent + offset
            ppid = 33393 if offset == 63 else pid + 1
            rows.append(f"{pid} {ppid} Thu Sep 10 20:05:30 2026")
        rows.append("33393 1 Sat Aug  1 17:17:22 2026")
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(Path(temporary), "\n".join(rows) + "\n")
        self.assertEqual(links, ())

    def test_ambiguous_claude_registry_matches_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            (registry / "duplicate.json").write_bytes(registry_file.read_bytes())
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_relative_registry_cwd_is_refused_even_when_it_contains_valid_omx_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, project = _write_provider_evidence(root)
            record = json.loads(registry_file.read_text(encoding="utf-8"))
            record["cwd"] = "relative-project"
            registry_file.write_text(json.dumps(record), encoding="utf-8")
            relative_state = root / "relative-project" / ".omx" / "state"
            relative_state.mkdir(parents=True)
            (relative_state / "session.json").write_bytes(
                (project / ".omx" / "state" / "session.json").read_bytes()
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                links = wd_process_lineage.discover_process_lineage(
                    _metadata(),
                    registry,
                    runner=_valid_runner,
                    observed_at=OBSERVED_AT,
                    local_timezone=LOCAL_TIMEZONE,
                )
            finally:
                os.chdir(previous)
        self.assertEqual(links, ())

    def test_malformed_registry_proc_start_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            record = json.loads(registry_file.read_text(encoding="utf-8"))
            record["procStart"] = "2026-09-10T17:05:34Z"
            registry_file.write_text(json.dumps(record), encoding="utf-8")
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_boolean_claude_registry_pid_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            record = json.loads(registry_file.read_text(encoding="utf-8"))
            record["pid"] = True
            registry_file.write_text(json.dumps(record), encoding="utf-8")

            def runner(*args, **kwargs):
                return _completed(
                    "1 33393 Thu Sep 10 20:05:34 2026\n"
                    "33393 0 Sat Aug  1 17:17:22 2026\n"
                )

            links = wd_process_lineage.discover_process_lineage(
                _metadata(),
                registry,
                runner=runner,
                observed_at=OBSERVED_AT,
                local_timezone=LOCAL_TIMEZONE,
            )
        self.assertEqual(links, ())

    def test_empty_omx_native_session_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            path = project / ".omx" / "state" / "session.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["native_session_id"] = ""
            path.write_text(json.dumps(document), encoding="utf-8")
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_naive_omx_started_at_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            path = project / ".omx" / "state" / "session.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["started_at"] = "2026-08-01T14:17:52"
            path.write_text(json.dumps(document), encoding="utf-8")
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_nonzero_ps_result_is_refused_even_with_valid_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, _ = _write_provider_evidence(root)

            def runner(*args, **kwargs):
                result = _valid_runner(*args, **kwargs)
                return subprocess.CompletedProcess(result.args, 1, result.stdout, "denied")

            links = wd_process_lineage.discover_process_lineage(
                _metadata(),
                registry,
                runner=runner,
                observed_at=OBSERVED_AT,
                local_timezone=LOCAL_TIMEZONE,
            )
        self.assertEqual(links, ())

    def test_duplicate_process_pid_rows_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary),
                "33394 33393 Thu Sep 10 20:05:33 2026\n"
                "33394 33393 Thu Sep 10 20:05:34 2026\n"
                "33393 1 Sat Aug  1 17:17:22 2026\n",
            )
        self.assertEqual(links, ())

    def test_missing_child_process_row_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary), "33393 1 Sat Aug  1 17:17:22 2026\n"
            )
        self.assertEqual(links, ())

    def test_missing_omx_process_row_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary), "33394 33393 Thu Sep 10 20:05:34 2026\n"
            )
        self.assertEqual(links, ())

    def test_repeated_process_chain_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            links = _discover(
                Path(temporary),
                "33394 40000 Thu Sep 10 20:05:34 2026\n"
                "40000 40001 Thu Sep 10 20:05:30 2026\n"
                "40001 40000 Thu Sep 10 20:05:29 2026\n"
                "33393 1 Sat Aug  1 17:17:22 2026\n",
            )
        self.assertEqual(links, ())

    def test_repeated_process_chain_stops_at_first_repeat(self) -> None:
        class CountingTable(dict):
            def __init__(self):
                super().__init__(
                    {
                        33394: (40000, OBSERVED_AT),
                        40000: (40001, OBSERVED_AT),
                        40001: (40000, OBSERVED_AT),
                    }
                )
                self.lookups = 0

            def get(self, key, default=None):
                self.lookups += 1
                return super().get(key, default)

        table = CountingTable()
        self.assertFalse(wd_process_lineage._has_ancestor(33394, 33393, table))
        self.assertEqual(table.lookups, 3)

    def test_process_chain_stops_at_pid_one(self) -> None:
        class CountingTable(dict):
            def __init__(self):
                super().__init__(
                    {
                        33394: (1, OBSERVED_AT),
                        1: (33394, OBSERVED_AT),
                    }
                )
                self.lookups = 0

            def get(self, key, default=None):
                self.lookups += 1
                return super().get(key, default)

        table = CountingTable()
        self.assertFalse(wd_process_lineage._has_ancestor(33394, 33393, table))
        self.assertEqual(table.lookups, 1)

    def test_registry_session_id_must_match_a_loaded_claude_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            record = json.loads(registry_file.read_text(encoding="utf-8"))
            record["sessionId"] = "different-claude-session"
            registry_file.write_text(json.dumps(record), encoding="utf-8")
            calls = 0

            def forbidden_runner(*args, **kwargs):
                nonlocal calls
                calls += 1
                return _valid_runner(*args, **kwargs)

            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=forbidden_runner
            )
        self.assertEqual(links, ())
        self.assertEqual(calls, 0)

    def test_non_claude_row_cannot_become_process_confirmed_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, _ = _write_provider_evidence(root)

            def forbidden_runner(*args, **kwargs):
                self.fail("ps ran without a loaded Claude candidate")

            with mock.patch.object(
                wd_process_lineage.os,
                "scandir",
                side_effect=AssertionError("registry scanned without Claude candidate"),
            ):
                links = wd_process_lineage.discover_process_lineage(
                    {
                        ("codex", "/synthetic/child.jsonl"): wd_models.SessionMetadata(
                            session_id="claude-child"
                        )
                    },
                    registry,
                    runner=forbidden_runner,
                )
        self.assertEqual(links, ())

    def test_claude_registry_scan_stops_at_entry_bound(self) -> None:
        class Entry:
            def __init__(self, name, path=None):
                self.name = name
                self.path = str(path or name)
                self.stat_calls = 0

            def stat(self, *, follow_symlinks):
                self.stat_calls += 1
                return Path(self.path).stat(follow_symlinks=follow_symlinks)

        class Scan:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            skipped = [Entry(f"skip-{index}.txt") for index in range(512)]
            bounded_out = Entry("33394.json", registry_file)
            calls = 0

            def runner(*args, **kwargs):
                nonlocal calls
                calls += 1
                return _valid_runner(*args, **kwargs)

            with mock.patch.object(
                wd_process_lineage.os,
                "scandir",
                return_value=Scan([*skipped, bounded_out]),
            ):
                links = wd_process_lineage.discover_process_lineage(
                    _metadata(), registry, runner=runner
                )

        self.assertEqual(links, ())
        self.assertEqual(bounded_out.stat_calls, 0)
        self.assertEqual(calls, 0)

    def test_claude_registry_reading_stops_when_total_byte_budget_is_spent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            filler = registry / "newer.json"
            filler.write_bytes(b" ")
            os.utime(registry_file, (1, 1))
            os.utime(filler, (2, 2))
            opened: list[Path] = []
            original_open = Path.open

            def recording_open(path, *args, **kwargs):
                opened.append(path)
                return original_open(path, *args, **kwargs)

            with (
                mock.patch.object(wd_models, "MAX_CLAUDE_REGISTRY_BYTES", 1),
                mock.patch.object(Path, "open", recording_open),
            ):
                links = wd_process_lineage.discover_process_lineage(
                    _metadata(), registry, runner=_valid_runner
                )

        self.assertEqual(links, ())
        self.assertEqual(opened, [filler])

    def test_oversized_claude_registry_record_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            registry_file.write_bytes(
                registry_file.read_bytes()
                + b" " * (wd_models.MAX_OMX_SESSION_BYTES + 1)
            )
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_malformed_claude_registry_record_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, registry_file, _ = _write_provider_evidence(root)
            registry_file.write_text("{", encoding="utf-8")
            links = wd_process_lineage.discover_process_lineage(
                _metadata(), registry, runner=_valid_runner
            )
        self.assertEqual(links, ())

    def test_probe_skips_children_with_higher_precedence_parentage(self) -> None:
        for name, child in {
            "embedded-or-tracking": wd_models.SessionMetadata(
                session_id="claude-child", parent_session_id="native-parent"
            ),
            "external-registry": wd_models.SessionMetadata(
                session_id="claude-child",
                external_parent_key=("codex", "/synthetic/parent"),
            ),
        }.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                registry, _, _ = _write_provider_evidence(root)
                calls = 0

                def forbidden_runner(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    return _valid_runner(*args, **kwargs)

                links = wd_process_lineage.discover_process_lineage(
                    {
                        ("claude", "/synthetic/claude.jsonl"): child,
                        ("codex", "/synthetic/codex.jsonl"): wd_models.SessionMetadata(
                            session_id="codex-orchestrator"
                        ),
                    },
                    registry,
                    runner=forbidden_runner,
                )
                self.assertEqual(links, ())
                self.assertEqual(calls, 0)

    def test_runner_failures_leave_metadata_byte_identical_to_no_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            child, parent = _write_transcripts(root, project)
            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(
                    wd_metadata, "external_lineage_registry_path", return_value=root / "missing.json"
                ),
                mock.patch.object(wd_metadata, "codex_sqlite_metadata_batch", return_value={}),
            ):
                baseline = wd_metadata.load_dashboard_metadata([child, parent])

                def raises(*args, **kwargs):
                    raise OSError("synthetic denial")

                def times_out(*args, **kwargs):
                    raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

                runners = {
                    "raises": raises,
                    "times-out": times_out,
                    "garbage": lambda *a, **k: _completed("not a process table\n"),
                    "empty": lambda *a, **k: _completed(""),
                    "malformed-row": lambda *a, **k: _completed(
                        "33394 33393 Thu Sep 10 20:05:34 2026\nbroken\n"
                    ),
                }
                for name, runner in runners.items():
                    with self.subTest(name=name):
                        retained: list[dict[str, object]] = []
                        result = wd_metadata.load_dashboard_metadata(
                            [child, parent],
                            injected_links=retained,
                            process_runner=runner,
                            process_observed_at=OBSERVED_AT,
                            process_local_timezone=LOCAL_TIMEZONE,
                        )
                        self.assertEqual(pickle.dumps(result), pickle.dumps(baseline))
                        self.assertEqual(retained, [])

    def test_process_lineage_changes_only_display_lineage_not_safety_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry, _, project = _write_provider_evidence(root)
            child, parent = _write_transcripts(root, project)
            items = [child, parent]
            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(
                    wd_metadata, "external_lineage_registry_path", return_value=root / "missing.json"
                ),
                mock.patch.object(wd_metadata, "codex_sqlite_metadata_batch", return_value={}),
            ):
                baseline_metadata = wd_metadata.load_dashboard_metadata(items)
                linked_metadata = wd_metadata.load_dashboard_metadata(
                    items,
                    injected_links=[],
                    process_runner=_valid_runner,
                    process_observed_at=OBSERVED_AT,
                    process_local_timezone=LOCAL_TIMEZONE,
                )
            activity = [
                (child, datetime(2026, 9, 10, 17, 5, 35, tzinfo=timezone.utc)),
                (parent, datetime(2026, 8, 1, 14, 17, 52, tzinfo=timezone.utc)),
            ]
            cfg = wd_models.Config(idle_minutes=60)
            baseline = wd_dashboard.make_dashboard_snapshot(
                OBSERVED_AT, cfg, items, activity, 0, 60, baseline_metadata
            )
            linked = wd_dashboard.make_dashboard_snapshot(
                OBSERVED_AT, cfg, items, activity, 0, 60, linked_metadata
            )

        self.assertEqual(
            (baseline.watched_count, baseline.holding_count, baseline.session_quiet),
            (2, 1, False),
        )
        self.assertEqual(
            (
                linked.watched_count,
                linked.holding_count,
                linked.session_quiet,
            ),
            (2, 1, False),
        )
        self.assertEqual(
            [(row.holding, row.quiet_remaining, row.last_event) for row in baseline.rows],
            [
                (True, 335.0, datetime(2026, 9, 10, 17, 5, 35, tzinfo=timezone.utc)),
                (False, 0.0, datetime(2026, 8, 1, 14, 17, 52, tzinfo=timezone.utc)),
            ],
        )
        self.assertEqual(
            [(row.holding, row.quiet_remaining, row.last_event) for row in linked.rows],
            [(row.holding, row.quiet_remaining, row.last_event) for row in baseline.rows],
        )
        self.assertIsNone(baseline.rows[0].external_parent_key)
        self.assertIsNotNone(linked.rows[0].external_parent_key)
        self.assertNotEqual(baseline.rows[0].details, linked.rows[0].details)

    def test_registry_file_precedes_retained_process_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_dir, _, project = _write_provider_evidence(root)
            child, process_parent = _write_transcripts(root, project)
            registry_parent_path = root / "registry-parent.jsonl"
            registry_parent_path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-08-01T14:17:52Z",
                        "payload": {
                            "id": "registry-parent",
                            "cwd": str(project),
                            "source": "cli",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            registry_parent = wd_models.ActivityFile(registry_parent_path, "codex")
            lineage_file = root / "lineage.json"
            lineage_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "links": [
                            {
                                "child": {
                                    "source": "claude",
                                    "session_id": "claude-child",
                                },
                                "parent": {
                                    "source": "codex",
                                    "session_id": "registry-parent",
                                },
                                "evidence": "synthetic registry declaration",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            retained = [
                {
                    "child": {"source": "claude", "session_id": "claude-child"},
                    "parent": {
                        "source": "codex",
                        "session_id": "codex-orchestrator",
                    },
                    "evidence": "earlier process confirmation",
                }
            ]
            with (
                mock.patch.object(
                    wd_config, "claude_sessions_dir", return_value=registry_dir
                ),
                mock.patch.object(
                    wd_metadata,
                    "external_lineage_registry_path",
                    return_value=lineage_file,
                ),
                mock.patch.object(
                    wd_metadata, "codex_sqlite_metadata_batch", return_value={}
                ),
            ):
                result = wd_metadata.load_dashboard_metadata(
                    [child, process_parent, registry_parent],
                    injected_links=retained,
                    process_runner=_valid_runner,
                    process_observed_at=OBSERVED_AT,
                    process_local_timezone=LOCAL_TIMEZONE,
                )

        self.assertEqual(
            result[wd_metadata.target_key(child)].external_parent_key,
            wd_metadata.target_key(registry_parent),
        )


if __name__ == "__main__":
    unittest.main()
