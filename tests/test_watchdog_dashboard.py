#!/usr/bin/env python3
"""Focused tests for claude-watchdog's terminal dashboard."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


from claude_watchdog import models as wd_models
from claude_watchdog import text as wd_text
from claude_watchdog import presentation as wd_presentation
from claude_watchdog import config as wd_config
from claude_watchdog import activity as wd_activity
from claude_watchdog import metadata as wd_metadata
from claude_watchdog import dashboard as wd_dashboard
from claude_watchdog import reporting as wd_reporting
from claude_watchdog import power as wd_power
from claude_watchdog import app as wd_app
NOW = datetime(2026, 9, 5, 0, 20, tzinfo=timezone.utc)


class FakeTTY(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class FakeScreen:
    def __init__(self, height=24, width=120, keys=()):
        self.height = height
        self.width = width
        self.keys = list(keys)
        self.lines = {}
        self.nodelay_value = None
        self.erase_calls = 0
        self.clear_calls = 0

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.erase_calls += 1
        self.lines.clear()

    def clear(self):
        self.clear_calls += 1
        self.lines.clear()

    def addnstr(self, y, x, text, count, *attrs):
        self.lines[(y, x)] = text[:count]

    def refresh(self):
        pass

    def nodelay(self, value):
        self.nodelay_value = value

    def keypad(self, value):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else -1


def _item(name="rollout-a.jsonl", source="codex", identities=frozenset()):
    return wd_models.ActivityFile(Path(name), source, identities=identities)


def _metadata(**values):
    defaults = dict(
        client="unknown",
        task="unknown",
        model="unknown",
        effort="unknown",
        started=None,
        cwd="unknown",
        provenance="unknown",
    )
    defaults.update(values)
    return wd_models.SessionMetadata(**defaults)


def _snapshot(items=None, metadata=None):
    items = items or [_item()]
    activity = [(items[0], NOW - timedelta(seconds=5))]
    return wd_dashboard.make_dashboard_snapshot(
        now=NOW,
        cfg=wd_models.Config(idle_minutes=30, user_idle_minutes=5),
        watch_set=items,
        activity=activity,
        user_idle=0,
        next_poll_seconds=42,
        metadata=metadata or {wd_metadata.target_key(items[0]): _metadata(task="Dashboard")},
    )


class DisplaySelectionTests(unittest.TestCase):
    def test_auto_uses_dashboard_only_for_capable_ttys(self):
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            self.assertEqual(wd_dashboard.resolve_display("auto", FakeTTY(), FakeTTY()), "dashboard")
            self.assertEqual(wd_dashboard.resolve_display("auto", FakeTTY(False), FakeTTY()), "log")
            self.assertEqual(wd_dashboard.resolve_display("auto", FakeTTY(), FakeTTY(False)), "log")
        with mock.patch.dict(os.environ, {"TERM": "dumb"}, clear=False):
            self.assertEqual(wd_dashboard.resolve_display("auto", FakeTTY(), FakeTTY()), "log")

    def test_explicit_modes_are_preserved(self):
        self.assertEqual(wd_dashboard.resolve_display("log", FakeTTY(), FakeTTY()), "log")
        self.assertEqual(wd_dashboard.resolve_display("dashboard", FakeTTY(False), FakeTTY(False)), "dashboard")

    def test_parse_args_exposes_display_and_no_color(self):
        cfg = wd_config.parse_args(["--display", "log", "--no-color"])
        self.assertEqual(cfg.display, "log")
        self.assertTrue(cfg.no_color)

    def test_task_labels_default_to_prompt_and_allow_metadata_override(self):
        self.assertEqual(wd_models.Config().task_label, "prompt")
        self.assertEqual(wd_config.parse_args([]).task_label, "prompt")
        self.assertEqual(
            wd_config.parse_args(["--task-label", "metadata"]).task_label,
            "metadata",
        )

    def test_no_color_environment_disables_colors(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertFalse(wd_presentation.colors_enabled(wd_models.Config()))


class MetadataAndSafetyTests(unittest.TestCase):
    def test_opencode_metadata_describes_only_watched_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER, model TEXT, agent TEXT)")
                db.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
                for sid, parent, title in (("root", None, "Build UI"), ("child", "root", "Map attack surface"), ("outside", None, "UNRELATED")):
                    db.execute("INSERT INTO session VALUES (?, ?, ?, '/repo', 1000, 2000, ?, 'build')", (sid, parent, title, json.dumps({"id": "muse-model", "variant": "high"})))
                db.execute("UPDATE session SET model=NULL WHERE id='child'")
                db.execute("INSERT INTO message VALUES ('m', 'child', 3000, ?)", (json.dumps({"role": "assistant", "modelID": "opus-model", "variant": "medium", "agent": "explore", "content": "PRIVATE BODY"}),))
            item = _item(str(path), "opencode", frozenset({"root"}))
            result = wd_metadata.load_dashboard_metadata([item])[wd_metadata.target_key(item)]
            self.assertEqual(result.client, "OpenCode")
            self.assertIn("Build UI", result.task)
            self.assertIn("Map attack surface", result.task)
            self.assertNotIn("UNRELATED", repr(result))
            self.assertNotIn("PRIVATE BODY", repr(result))
            self.assertIn("opus-model / medium", "\n".join(result.details))
            self.assertIn("muse-model / high", "\n".join(result.details))
            snapshot = _snapshot([item], {wd_metadata.target_key(item): result})
            self.assertEqual(snapshot.rows[0].details, result.details)
            self.assertEqual(snapshot.watched_count, 1)

    def test_opencode_model_switch_uses_current_session_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE session (id TEXT, parent_id TEXT, title TEXT, time_created INTEGER, time_updated INTEGER, model TEXT)")
                db.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
                db.execute("INSERT INTO session VALUES ('root', NULL, 'Current task', 1000, 2000, ?)", (json.dumps({"id": "new-model", "variant": "medium"}),))
                db.execute("INSERT INTO session VALUES ('child', 'root', 'Review', 1000, 3000, ?)", (json.dumps({"id": "child-model", "variant": "high"}),))
                db.execute("INSERT INTO message VALUES ('old', 'root', 1500, ?)", (json.dumps({"role": "assistant", "modelID": "old-model", "variant": "max"}),))
            result = wd_metadata.opencode_metadata(_item(str(path), "opencode", frozenset({"root"})))
            self.assertEqual((result.model, result.effort), ("new-model", "medium"))
            root = next(child for child in result.children if child.session_id == "root")
            self.assertEqual((root.model, root.effort), ("new-model", "medium"))
            self.assertNotIn("mixed", result.model)

    def test_opencode_missing_metadata_schema_keeps_group_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE session (id TEXT, parent_id TEXT)")
            result = wd_metadata.load_session_metadata(_item(str(path), "opencode", frozenset({"root"})))
            self.assertEqual(result.task, "1 lineage seeds")

    def test_omx_client_requires_exact_launch_evidence_and_survives_pointer_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = '11111111-2222-4333-8444-555555555555'
            path = root / 'rollout.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': sid, 'timestamp': NOW.isoformat(), 'cwd': str(root), 'originator': 'codex-tui'}}) + '\n')
            state = root / '.omx/state'
            state.mkdir(parents=True)
            pointer = state / 'session.json'
            pointer.write_text(json.dumps({'native_session_id': sid, 'session_id': 'omx-launch'}))
            self.assertEqual(wd_metadata.jsonl_metadata(_item(str(path))).client, 'OMX / Codex')
            pointer.write_text(json.dumps({'native_session_id': 'other', 'session_id': 'omx-other'}))
            self.assertEqual(wd_metadata.jsonl_metadata(_item(str(path))).client, 'codex-tui')
            logs = root / '.omx/logs'
            logs.mkdir()
            log = logs / (NOW.strftime('omx-%Y-%m-%d.jsonl'))
            log.write_text(json.dumps({'event': 'session_start_reconciled', 'native_session_id': sid,
                'session_id': 'omx-launch', 'timestamp': NOW.isoformat()}) + '\n')
            self.assertEqual(wd_metadata.jsonl_metadata(_item(str(path))).client, 'OMX / Codex')

    def test_omx_files_or_hooks_alone_do_not_relabel_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'rollout.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': 'session-a', 'timestamp': NOW.isoformat(), 'cwd': str(root), 'originator': 'codex-tui'}}) + '\n')
            state = root / '.omx/state'
            state.mkdir(parents=True)
            (state/'session.json').write_text(json.dumps({'native_session_id': 'session-a', 'session_id': 'session-a'}))
            logs = root / '.omx/logs'
            logs.mkdir()
            (logs/NOW.strftime('omx-%Y-%m-%d.jsonl')).write_text(json.dumps({
                'event': 'notify_hook', 'native_session_id': 'session-a', 'session_id': 'omx-other'})+'\n')
            self.assertEqual(wd_metadata.jsonl_metadata(_item(str(path))).client, 'codex-tui')
            (state/'session.json').write_text('{broken')
            self.assertEqual(wd_metadata.jsonl_metadata(_item(str(path))).client, 'codex-tui')

    def test_sanitize_removes_terminal_controls_bidi_and_newlines(self):
        value = "safe\x1b[31m\nBAD\t\u202etxt\x9b"
        cleaned = wd_text.sanitize_terminal_text(value)
        self.assertEqual(cleaned, "safe[31m BAD txt")
        self.assertFalse(any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in cleaned))

    def test_unicode_clipping_counts_terminal_cells_and_uses_ellipsis(self):
        self.assertEqual(wd_text.clip_cells("ab界cd", 5), "ab界…")
        self.assertLessEqual(wd_text.text_cells(wd_text.clip_cells("abcdef", 4)), 4)

    def test_jsonl_metadata_uses_explicit_fields_without_prompt_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            records = [
                {"timestamp": "2026-09-05T00:01:00Z", "type": "session_meta", "payload": {
                    "originator": "Codex Desktop", "cwd": "/tmp/project", "agent_nickname": "Ada"}},
                {"timestamp": "2026-09-05T00:02:00Z", "type": "turn_context", "payload": {
                    "model": "gpt-5", "reasoning_effort": "high", "user_prompt": "SECRET"}},
            ]
            path.write_text("".join(json.dumps(x) + "\n" for x in records), encoding="utf-8")
            result = wd_metadata.jsonl_metadata(_item(str(path)))
        self.assertEqual(result.client, "Codex Desktop")
        self.assertEqual(result.model, "gpt-5")
        self.assertEqual(result.effort, "high")
        self.assertNotIn("SECRET", repr(result))

    def test_claude_jsonl_metadata_uses_explicit_top_level_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-session.jsonl"
            records = [
                {
                    "timestamp": "2026-09-05T00:01:00Z",
                    "type": "user",
                    "entrypoint": "cli",
                    "cwd": "/tmp/claude-project",
                    "message": {"content": "SECRET USER PROMPT"},
                },
                {
                    "timestamp": "2026-09-05T00:02:00Z",
                    "type": "assistant",
                    "effort": "high",
                    "message": {
                        "model": "claude-opus-5",
                        "content": "SECRET ASSISTANT RESPONSE",
                    },
                },
                {
                    "timestamp": "2026-09-05T00:03:00Z",
                    "type": "ai-title",
                    "aiTitle": "Load average explanation",
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            for profile_id, profile_label, expected_client in (
                (None, None, "Claude Code"),
                ("work", "Work", "Claude Code [Work]"),
            ):
                with self.subTest(profile=profile_id):
                    item = wd_models.ActivityFile(
                        path, "claude", profile_id=profile_id, profile_label=profile_label
                    )
                    result = wd_metadata.jsonl_metadata(item)
                    self.assertEqual(result.client, expected_client)
                    self.assertEqual(result.task, "Load average explanation")
                    self.assertEqual(result.model, "claude-opus-5")
                    self.assertEqual(result.effort, "high")
                    self.assertEqual(result.cwd, "/tmp/claude-project")
                    self.assertNotIn("SECRET", repr(result))

    def test_profile_keeps_recorded_model_and_ignores_vanilla_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile" / "project" / "session.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "type": "assistant", "sessionId": "profile-session", "entrypoint": "sdk-cli",
                "timestamp": "2026-09-05T00:02:00Z", "effort": "medium",
                "message": {"model": "gpt-example", "content": []},
            }) + "\n", encoding="utf-8")
            item = wd_models.ActivityFile(path, "claude", profile_id="work", profile_label="Work")
            with mock.patch.object(wd_metadata, "_claude_registry_name", side_effect=AssertionError("vanilla registry read")):
                result = wd_metadata.jsonl_metadata(item)
            self.assertEqual(result.client, "sdk-cli [Work]")
            self.assertEqual(result.model, "gpt-example")
            self.assertEqual(result.effort, "medium")
            self.assertEqual(result.session_id, "profile-session")

    def test_claude_custom_title_overrides_later_generated_title_and_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = {"type": "custom-title", "customTitle": "Chosen\nTitle\x1b[31m"}
            generated = {"type": "ai-title", "aiTitle": "Generated title"}
            for index, records in enumerate(([custom, generated], [generated, custom])):
                path = Path(tmp) / f"claude-titles-{index}.jsonl"
                path.write_text(
                    "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
                )
                with self.subTest(order=index):
                    result = wd_metadata.jsonl_metadata(_item(str(path), source="claude"))
                    self.assertEqual(result.task, "Chosen Title[31m")

    def test_claude_explicit_title_between_large_records_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude-desktop-large.jsonl"
            metadata = json.dumps({
                "type": "user", "entrypoint": "claude-desktop",
                "sessionId": "desktop-session", "cwd": "/tmp/scratch",
            }).encode() + b"\n"
            large = b'{"type":"attachment","content":"' + b"x" * 128_000 + b'"}\n'
            title = json.dumps({"type": "custom-title", "customTitle": "Desktop task"}).encode() + b"\n"
            middle = b'{"type":"attachment","content":"' + b"m" * 92_000 + b'"}\n'
            assistant = json.dumps({
                "type": "assistant", "entrypoint": "claude-desktop",
                "sessionId": "desktop-session", "cwd": "/tmp/scratch",
                "effort": "high", "message": {"model": "claude-opus-5", "content": "PRIVATE"},
            }).encode() + b"\n"
            tail = b'{"type":"attachment","content":"' + b"y" * 179_000 + b'"}\n'
            path.write_bytes(metadata + large + title + middle + assistant + tail)
            result = wd_metadata.load_session_metadata(_item(str(path), source="claude"))
        self.assertEqual(result.task, "Desktop task")
        self.assertEqual((result.model, result.effort), ("claude-opus-5", "high"))
        self.assertNotIn("PRIVATE", repr(result))

    def test_untitled_claude_agent_uses_exact_registry_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "agent.jsonl"
            session_id = "22222222-3333-4444-8555-666666666666"
            transcript.write_text(json.dumps({
                "type": "user", "entrypoint": "sdk-cli", "sessionId": session_id,
                "cwd": "/repo/.claude/worktrees/example-notebook",
                "message": {"content": "PRIVATE TASK"},
            }) + "\n", encoding="utf-8")
            registry = root / "sessions"
            registry.mkdir()
            (registry / "42.json").write_text(json.dumps({
                "sessionId": session_id, "name": "example-notebook-c6",
                "nameSource": "derived", "entrypoint": "sdk-cli",
            }), encoding="utf-8")
            (registry / "43.json").write_text(json.dumps({
                "sessionId": "other", "name": "wrong-agent",
            }), encoding="utf-8")
            with mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry):
                result = wd_metadata.load_session_metadata(_item(str(transcript), source="claude"))
        self.assertEqual(result.task, "example-notebook-c6")
        self.assertNotIn("PRIVATE", repr(result))

    def test_claude_registry_budget_counts_actual_small_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "sessions"
            registry.mkdir()
            session_id = "target-session"
            target = registry / "target.json"
            target.write_text(json.dumps({
                "sessionId": session_id, "name": "older-agent-name",
            }), encoding="utf-8")
            base_ns = 1_700_000_000_000_000_000
            os.utime(target, ns=(base_ns, base_ns))
            for index in range(40):
                decoy = registry / f"decoy-{index}.json"
                decoy.write_text(json.dumps({
                    "sessionId": f"decoy-{index}", "name": f"decoy-{index}",
                }), encoding="utf-8")
                timestamp = base_ns + index + 1
                os.utime(decoy, ns=(timestamp, timestamp))
            with mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry):
                name = wd_metadata._claude_registry_name(session_id)
        self.assertEqual(name, "older-agent-name")

    def test_claude_registry_caps_empty_file_attempts_independently_of_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "sessions"
            registry.mkdir()
            for index in range(10):
                (registry / f"empty-{index}.json").touch()
            opened = []
            original_open = Path.open

            def tracking_open(path, *args, **kwargs):
                opened.append(path)
                return original_open(path, *args, **kwargs)

            with (
                mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry),
                mock.patch.object(wd_models, "MAX_CLAUDE_REGISTRY_FILES", 3),
                mock.patch.object(Path, "open", tracking_open),
            ):
                name = wd_metadata._claude_registry_name("missing-session")
        self.assertIsNone(name)
        self.assertEqual(len(opened), 3)

    def test_prompt_task_label_is_default_and_metadata_mode_suppresses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "agent.jsonl"
            session_id = "abcdef01-2222-4333-8444-555555555555"
            records = [
                {"type": "user", "entrypoint": "sdk-cli", "sessionId": session_id,
                 "cwd": "/repo/.claude/worktrees/example-notebook",
                 "message": {"content": "PRIVATE RAW MESSAGE"}},
                {"type": "last-prompt", "sessionId": session_id,
                 "lastPrompt": "Review\nmodel picker\x1b[2J"},
                {"type": "last-prompt", "sessionId": session_id,
                 "lastPrompt": "Verify final model picker behavior"},
            ]
            transcript.write_text("".join(json.dumps(record) + "\n" for record in records))
            registry = root / "missing-registry"
            with mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry):
                private = wd_metadata.load_session_metadata(
                    _item(str(transcript), source="claude"), task_label="metadata"
                )
                item = _item(str(transcript), source="claude")
                prompt = wd_metadata.load_dashboard_metadata([item])[wd_metadata.target_key(item)]
        self.assertEqual(private.task, "example-notebook · abcdef01")
        self.assertEqual(prompt.task, "Verify final model picker behavior")
        self.assertEqual(prompt.provenance, "jsonl-prompt")
        self.assertNotIn("PRIVATE RAW", repr(prompt))

    def test_latest_prompt_label_is_sanitized_and_clipped_to_terminal_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "agent.jsonl"
            session_id = "12345678-abcd"
            selected_prompt = "Review\n\x1b[2J" + ("界e\u0301" * 100)
            transcript.write_text("".join(json.dumps(record) + "\n" for record in (
                {"type": "user", "entrypoint": "sdk-cli", "sessionId": session_id,
                 "cwd": "/repo/project"},
                {"type": "last-prompt", "sessionId": session_id,
                 "lastPrompt": selected_prompt},
            )), encoding="utf-8")
            with mock.patch.object(wd_config, "claude_sessions_dir", return_value=root / "missing-registry"
            ):
                result = wd_metadata.load_session_metadata(
                    _item(str(transcript), source="claude"), task_label="prompt"
                )
        self.assertEqual(result.provenance, "jsonl-prompt")
        self.assertNotIn("\x1b", result.task)
        self.assertNotIn("\n", result.task)
        self.assertLessEqual(wd_text.text_cells(result.task), 160)
        self.assertTrue(result.task.endswith("…"))

    def test_claude_spread_sampling_reaches_latest_dense_tail_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "dense-tail.jsonl"
            session_id = "12345678-abcd"
            first = json.dumps({
                "type": "user", "entrypoint": "sdk-cli", "sessionId": session_id,
                "cwd": "/repo/project",
            }).encode() + b"\n"
            large = b'{"type":"attachment","content":"' + b"x" * 260_000 + b'"}\n'
            noise = b"".join(
                json.dumps({"type": "noise", "n": index}).encode() + b"\n"
                for index in range(600)
            )
            assistant = json.dumps({
                "type": "assistant", "sessionId": session_id, "effort": "high",
                "message": {"model": "claude-opus-5"},
            }).encode() + b"\n"
            latest = json.dumps({
                "type": "last-prompt", "sessionId": session_id,
                "lastPrompt": "EXPECTED LATEST",
            }).encode() + b"\n"
            transcript.write_bytes(first + large + noise + assistant + latest)
            with mock.patch.object(wd_config, "claude_sessions_dir", return_value=root / "missing-registry"
            ):
                result = wd_metadata.load_session_metadata(
                    _item(str(transcript), source="claude"), task_label="prompt"
                )
        self.assertEqual(result.task, "EXPECTED LATEST")
        self.assertEqual((result.model, result.effort), ("claude-opus-5", "high"))

    def test_explicit_claude_title_outranks_prompt_mode_and_project_fallback_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "missing-registry"
            titled = root / "titled.jsonl"
            titled.write_text("".join(json.dumps(record) + "\n" for record in (
                {"type": "user", "entrypoint": "sdk-cli", "sessionId": "session-title",
                 "cwd": "/repo/project"},
                {"type": "custom-title", "customTitle": "Chosen title"},
                {"type": "last-prompt", "lastPrompt": "Private task"},
            )))
            untitled = root / "untitled.jsonl"
            untitled.write_text(json.dumps({
                "type": "user", "entrypoint": "sdk-cli",
                "sessionId": "12345678-abcd", "cwd": "/repo/project",
            }) + "\n")
            with mock.patch.object(wd_config, "claude_sessions_dir", return_value=registry):
                title_result = wd_metadata.load_session_metadata(
                    _item(str(titled), source="claude"), task_label="prompt"
                )
                fallback_result = wd_metadata.load_session_metadata(
                    _item(str(untitled), source="claude")
                )
        self.assertEqual(title_result.task, "Chosen title")
        self.assertEqual(title_result.provenance, "jsonl")
        self.assertEqual(fallback_result.task, "project · 12345678")

    def test_claude_top_level_metadata_is_scoped_to_claude_family_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-scoped.jsonl"
            path.write_text(json.dumps({
                "type": "ai-title",
                "entrypoint": "cli",
                "cwd": "/tmp/claude-project",
                "effort": "high",
                "aiTitle": "Claude title",
            }) + "\n", encoding="utf-8")
            result = wd_metadata.jsonl_metadata(_item(str(path), source="codex"))
        self.assertEqual(
            (result.client, result.task, result.effort, result.cwd),
            ("unknown", "unknown", "unknown", "unknown"),
        )

    def test_claude_top_level_metadata_ignores_non_string_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "malformed-claude.jsonl"
            records = [
                {
                    "type": "user",
                    "entrypoint": {"name": "cli"},
                    "cwd": ["/tmp/project"],
                    "message": {"content": "SECRET"},
                },
                {"type": "assistant", "effort": 3},
                {"type": "ai-title", "aiTitle": ["Generated title"]},
                {"type": "custom-title", "customTitle": {"title": "Chosen title"}},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            result = wd_metadata.jsonl_metadata(_item(str(path), source="claude"))
        self.assertEqual(
            (result.client, result.task, result.effort, result.cwd),
            ("unknown", "unknown", "unknown", "unknown"),
        )
        self.assertNotIn("SECRET", repr(result))

    def test_model_provider_is_not_displayed_as_model_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider-only.jsonl"
            path.write_text(json.dumps({
                "timestamp": NOW.isoformat(), "type": "session_meta",
                "payload": {"originator": "Codex Desktop", "model_provider": "openai"},
            }) + "\n", encoding="utf-8")
            result = wd_metadata.jsonl_metadata(_item(str(path)))
        self.assertEqual(result.model, "unknown")

    def test_unknown_metadata_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            result = wd_metadata.jsonl_metadata(_item(str(path)))
        self.assertEqual((result.task, result.model, result.effort), ("unknown", "unknown", "unknown"))

    def test_codex_sqlite_metadata_exactly_matches_rollout_path_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / "rollout.jsonl"
            rollout.write_text("{}\n", encoding="utf-8")
            database = sqlite3.connect(root / "state.sqlite")
            database.execute("CREATE TABLE threads (rollout_path TEXT, title TEXT, model TEXT, reasoning_effort TEXT, created_at INTEGER, source TEXT, cwd TEXT)")
            database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)", (str(rollout), "Build dashboard", "fable-5.1", "high", 1788566400, "app", "/repo"))
            database.commit()
            database.close()
            result = wd_metadata.codex_sqlite_metadata(_item(str(rollout)), root / "state.sqlite")
        self.assertEqual((result.task, result.model, result.effort), ("Build dashboard", "fable-5.1", "high"))
        self.assertEqual(result.provenance, "codex-state")

    def test_metadata_failure_is_display_only(self):
        item = _item(source="claude")
        with mock.patch.object(wd_metadata, "load_session_metadata", side_effect=RuntimeError("bad metadata")):
            values = wd_metadata.load_dashboard_metadata([item])
        self.assertEqual(values[wd_metadata.target_key(item)].task, "unknown")

    def test_unexpected_codex_batch_failure_keeps_jsonl_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(json.dumps({
                "timestamp": NOW.isoformat(), "type": "turn_context",
                "payload": {"model": "fallback-model", "reasoning_effort": "high"},
            }) + "\n", encoding="utf-8")
            item = _item(str(path))
            with mock.patch.object(wd_metadata, "codex_sqlite_metadata_batch", side_effect=RuntimeError("unexpected")):
                values = wd_metadata.load_dashboard_metadata([item])
        self.assertEqual(values[wd_metadata.target_key(item)].model, "fallback-model")

    def test_jsonl_metadata_read_has_a_fixed_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.jsonl"
            path.write_bytes(b'{"payload":"' + b"x" * (wd_models.MAX_METADATA_BYTES * 3) + b'"}\n')
            reads = []
            original = Path.open

            class TrackingReader:
                def __init__(self, handle):
                    self.handle = handle

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.handle.close()

                def seek(self, *args):
                    return self.handle.seek(*args)

                def read(self, size=-1):
                    reads.append(size)
                    return self.handle.read(size)

            with mock.patch.object(Path, "open", lambda value, *a, **kw: TrackingReader(original(value, *a, **kw))):
                wd_metadata.jsonl_metadata(_item(str(path)))
        self.assertLessEqual(sum(size for size in reads if size > 0), wd_models.MAX_METADATA_BYTES)

    def test_tail_metadata_is_read_even_when_head_has_many_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "many.jsonl"
            head = "".join(json.dumps({"type": "noise", "n": index}) + "\n" for index in range(250))
            tail = json.dumps({"type": "turn_context", "payload": {"model": "latest-model", "reasoning_effort": "xhigh"}}) + "\n"
            path.write_text(head + (" " * wd_models.READ_CHUNK_BYTES) + "\n" + tail, encoding="utf-8")
            result = wd_metadata.jsonl_metadata(_item(str(path)), max_records=20)
        self.assertEqual((result.model, result.effort), ("latest-model", "xhigh"))

    def test_non_claude_metadata_preserves_the_deep_tail_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deep-tail.jsonl"
            head = b'{"type":"noise","content":"' + b"h" * 249_000 + b'"}\n'
            metadata = json.dumps({
                "type": "turn_context",
                "payload": {"model": "deep-tail-model", "reasoning_effort": "high"},
            }).encode() + b"\n"
            tail = b'{"type":"noise","content":"' + b"t" * 99_000 + b'"}\n'
            path.write_bytes(head + metadata + tail)
            result = wd_metadata.jsonl_metadata(_item(str(path), source="codex"))
        self.assertEqual((result.model, result.effort), ("deep-tail-model", "high"))


class DashboardControllerTests(unittest.TestCase):
    def test_navigation_survives_redraw_and_refresh(self):
        for down, up in (("j", "k"), (258, 259)):
            with self.subTest(down=down):
                screen = FakeScreen(keys=[down, up])
                dashboard = wd_dashboard.TerminalDashboard(
                    screen, wd_models.Config(no_color=True), mock.Mock(A_REVERSE=1)
                )
                snapshot = _snapshot([_item("a"), _item("b")])
                dashboard.update(snapshot)
                dashboard.process_input()
                self.assertEqual(dashboard.state.selected, 1)
                dashboard.update(snapshot)
                self.assertEqual(dashboard.state.selected, 1)
                dashboard.process_input()
                self.assertEqual(dashboard.state.selected, 0)

    def test_process_input_reaches_last_rendered_row_with_synthesized_ancestor(self):
        root_item = _item(
            "rollout-2026-09-09T00-00-00-synthetic-root.jsonl"
        )
        child_item = _item(
            "rollout-2026-09-09T00-00-00-synthetic-child.jsonl"
        )
        snapshot = _snapshot(
            [root_item, child_item],
            {
                wd_metadata.target_key(root_item): _metadata(
                    task="Root",
                    session_id="synthetic-root",
                    lineage_namespace="synthetic-lineage",
                ),
                wd_metadata.target_key(child_item): _metadata(
                    task="Child",
                    session_id="synthetic-child",
                    parent_session_id="synthetic-leader",
                    lineage_namespace="synthetic-lineage",
                ),
            },
        )
        ancestor = replace(
            snapshot.rows[0],
            key=("codex", "synthetic-leader.jsonl"),
            task="Leader",
            path="synthetic-leader.jsonl",
            last_event=None,
            quiet_remaining=0.0,
            holding=False,
            session_id="synthetic-leader",
            parent_session_id="synthetic-root",
            display_only=True,
        )
        snapshot = replace(snapshot, display_rows=(ancestor,))
        screen = FakeScreen(keys=["j", "j", "j", "j"])
        dashboard = wd_dashboard.TerminalDashboard(
            screen, wd_models.Config(no_color=True), mock.Mock(A_REVERSE=1)
        )
        dashboard.state.sort = "title"

        dashboard.update(snapshot)
        for _ in range(4):
            dashboard.process_input()

        self.assertEqual(dashboard.state.selected, 2)
        self.assertEqual(dashboard.state.selected_key, snapshot.rows[1].key)

    def test_claude_nested_subagent_is_admitted_with_quiet_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "project" / "session.jsonl"
            child = root / "project" / "session" / "subagents" / "agent-one.jsonl"
            child.parent.mkdir(parents=True)
            parent.write_text(json.dumps({"timestamp": (NOW - timedelta(hours=1)).isoformat()}) + "\n")
            child.write_text(json.dumps({"timestamp": NOW.isoformat()}) + "\n")
            ignored = child.parent / "agent-one.meta.json"
            ignored.write_text("{}")
            with mock.patch.object(wd_config, "claude_projects_dir", return_value=root):
                selected = wd_activity.select_watch_set(wd_models.Config(source="claude"), now=NOW)
            self.assertEqual([item.path for item in selected], [child])

    def test_filter_changes_rows_but_not_guard_targets(self):
        first, second = _item("a.jsonl", "codex"), _item("b.jsonl", "claude")
        metadata = {
            wd_metadata.target_key(first): _metadata(task="Alpha"),
            wd_metadata.target_key(second): _metadata(task="Beta"),
        }
        snap = wd_dashboard.make_dashboard_snapshot(
            NOW, wd_models.Config(), [first, second],
            [(first, NOW), (second, NOW - timedelta(seconds=2))], 0, 10, metadata,
        )
        state = wd_models.DashboardState(query="beta")
        visible = wd_dashboard.visible_dashboard_rows(snap.rows, state)
        self.assertEqual([row.task for row in visible], ["Beta"])
        self.assertEqual(snap.watched_count, 2)

    def test_keys_filter_sort_provider_navigation_detail_clear_and_quit(self):
        state = wd_models.DashboardState()
        self.assertEqual(wd_dashboard.handle_dashboard_key(state, ord("/"), 4), "filter")
        wd_dashboard.handle_dashboard_key(state, ord("x"), 4)
        self.assertEqual(state.query, "x")
        wd_dashboard.handle_dashboard_key(state, 10, 4)
        self.assertFalse(state.filter_input)
        wd_dashboard.handle_dashboard_key(state, ord("s"), 4)
        self.assertEqual(state.sort, "title")
        wd_dashboard.handle_dashboard_key(state, ord("p"), 4)
        self.assertIsNotNone(state.source_filter)
        wd_dashboard.handle_dashboard_key(state, ord("j"), 4)
        self.assertEqual(state.selected, 1)
        wd_dashboard.handle_dashboard_key(state, ord("k"), 4)
        self.assertEqual(state.selected, 0)
        wd_dashboard.handle_dashboard_key(state, 10, 4)
        self.assertTrue(state.details)
        wd_dashboard.handle_dashboard_key(state, ord("c"), 4)
        self.assertEqual((state.query, state.source_filter), ("", None))
        with self.assertRaises(KeyboardInterrupt):
            wd_dashboard.handle_dashboard_key(state, ord("q"), 4)

    def test_h_hides_selected_target_and_clear_does_not_restore_it(self):
        path = Path("/tmp/claude-watchdog-hide-session.jsonl")
        state = wd_models.DashboardState(selected_key=("claude", str(path)))
        self.assertEqual(wd_dashboard.handle_dashboard_key(state, ord("h"), 1), "hide")
        self.assertEqual(state.hidden_paths, {path})
        wd_dashboard.handle_dashboard_key(state, ord("c"), 1)
        self.assertEqual(state.hidden_paths, {path})
        idle = wd_models.DashboardState()
        self.assertIsNone(wd_dashboard.handle_dashboard_key(idle, ord("h"), 0))
        self.assertEqual(idle.hidden_paths, set())

    def test_selection_is_retained_by_stable_target_key(self):
        a, b = _item("a.jsonl"), _item("b.jsonl")
        state = wd_models.DashboardState(selected_key=wd_metadata.target_key(b))
        wd_dashboard.retain_dashboard_selection(state, [_snapshot([b]).rows[0], _snapshot([a]).rows[0]])
        self.assertEqual(state.selected, 0)

    def test_identity_growth_does_not_change_display_target_key(self):
        original = _item("shared.jsonl", "omx", frozenset({"one"}))
        expanded = _item("shared.jsonl", "omx", frozenset({"one", "two"}))
        self.assertEqual(wd_metadata.target_key(original), wd_metadata.target_key(expanded))
        self.assertEqual(
            wd_app.dashboard_admission_notice([original], [expanded]),
            "admitted 1 new OMX identity",
        )

    def test_escape_cancels_filter_edit_and_special_keys_are_not_text(self):
        state = wd_models.DashboardState(query="alpha")
        wd_dashboard.handle_dashboard_key(state, ord("/"), 2)
        wd_dashboard.handle_dashboard_key(state, ord("x"), 2)
        wd_dashboard.handle_dashboard_key(state, 410, 2)  # curses KEY_RESIZE
        wd_dashboard.handle_dashboard_key(state, 27, 2)
        self.assertEqual(state.query, "alpha")
        self.assertFalse(state.filter_input)
        wd_dashboard.handle_dashboard_key(state, ord("/"), 2)
        wd_dashboard.handle_dashboard_key(state, "界", 2)
        wd_dashboard.handle_dashboard_key(state, 10, 2)
        self.assertEqual(state.query, "alpha界")


class RenderingTests(unittest.TestCase):
    def test_medium_widths_preserve_full_countdown(self):
        for width in (80, 81, 82, 90, 99):
            with self.subTest(width=width):
                row = next(line for line in wd_dashboard.dashboard_lines(_snapshot(), width=width) if "CODEX" in line)
                self.assertIn("29:55", row)
                self.assertLessEqual(wd_text.text_cells(row), width - 1)

    def test_wide_screen_can_donate_short_task_space_to_long_labels(self):
        item = _item()
        client = "OpenCode custom orchestration client"
        model = "model-" + "x" * 90
        snap = _snapshot(metadata={wd_metadata.target_key(item): _metadata(task="Task", client=client, model=model)})
        row = next(line for line in wd_dashboard.dashboard_lines(snap, width=210) if "CODEX" in line)
        self.assertIn(client, row)
        self.assertIn(model, row)

    def test_wide_screen_uses_space_for_full_model_and_task(self):
        item = _item()
        task = "Review all notebook exercises and validate the generated security report"
        model = "muse-spark-1.3-contributor-free"
        snap = _snapshot(metadata={wd_metadata.target_key(item): _metadata(task=task, model=model, effort="high")})
        screen = FakeScreen(width=210)
        dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(no_color=True))
        dashboard.update(snap)
        row = screen.lines[(3, 0)]
        self.assertIn(task, row)
        self.assertIn(model + " / high", row)
        self.assertGreater(wd_text.text_cells(row), 180)
        screen.width = 100
        dashboard.update(snap)
        self.assertLessEqual(wd_text.text_cells(screen.lines[(3, 0)]), 99)

    def test_group_details_appear_in_panel_and_exit_report(self):
        item = _item("db", "opencode", frozenset({"root"}))
        details = ("Build UI · builder · muse / high", "Audit · explore · opus / medium")
        metadata = _metadata(client="OpenCode", task="Build UI | Audit", details=details)
        snap = _snapshot([item], {wd_metadata.target_key(item): metadata})
        screen = FakeScreen(width=180)
        dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(no_color=True), mock.Mock(A_REVERSE=1))
        dashboard.state.details = True
        dashboard.update(snap)
        rendered = "\n".join(screen.lines.values())
        for detail in details:
            self.assertIn(detail, rendered)
        history = wd_reporting.WatchHistory()
        history.observe(snap)
        report = "\n".join(wd_reporting.exit_report_lines(history, wd_models.Config(), 130, ""))
        for detail in details:
            self.assertIn(detail, report)

    def test_client_and_model_colors_are_distinct_stable_and_selection_safe(self):
        curses = mock.Mock(COLORS=256, COLOR_PAIRS=256, A_REVERSE=1)
        curses.color_pair.side_effect = lambda pair: pair << 8
        screen = FakeScreen(width=120)
        screen.addnstr = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=True):
            dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(), curses)
        items = [_item("a"), _item("b"), _item("c")]
        metadata = {
            wd_metadata.target_key(item): _metadata(client=client, model=model, effort="high")
            for item, client, model in zip(items,
                ["Claude Code", "Codex Desktop", "codex-tui"],
                ["claude-opus-5", "gpt-6-astra", "gpt-5.6-sol"])
        }
        snapshot = _snapshot(items, metadata)
        dashboard.update(snapshot)
        cells = {(call.args[0], call.args[1]): call.args for call in screen.addnstr.call_args_list}
        clients = [cells[(y, 12)][4] & ~1 for y in (3, 4, 5)]
        model_x = next(x for (y, x), args in cells.items() if y == 3 and args[2].startswith("claude-opus-5"))
        models = [cells[(y, model_x)][4] & ~1 for y in (3, 4, 5)]
        self.assertEqual(len(set(clients)), 3)
        self.assertEqual(len(set(models)), 3)
        self.assertTrue(all(clients + models))
        self.assertEqual(cells[(3, 12)][4] & 1, 1)
        self.assertEqual(cells[(3, model_x)][4] & 1, 1)
        dashboard.state.query = "Codex Desktop"
        dashboard.update(snapshot)
        filtered = {call.args[1]: call.args for call in screen.addnstr.call_args_list if call.args[0] == 3}
        self.assertEqual(filtered[12][4] & ~1, clients[1])
        filtered_model = next(args for args in filtered.values() if args[2].startswith("gpt-6-astra"))
        self.assertEqual(filtered_model[4] & ~1, models[1])

    def test_model_color_in_medium_layout_and_monochrome_fallback(self):
        for no_color, available in ((False, True), (True, True), (False, False)):
            with self.subTest(no_color=no_color, available=available):
                curses = mock.Mock(COLORS=8, COLOR_PAIRS=8, A_REVERSE=1)
                curses.has_colors.return_value = available
                curses.color_pair.side_effect = lambda pair: pair << 8
                screen = FakeScreen(width=80)
                screen.addnstr = mock.Mock()
                with mock.patch.dict(os.environ, {}, clear=True):
                    dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(no_color=no_color), curses)
                dashboard.update(_snapshot(metadata={wd_metadata.target_key(_item()): _metadata(model="gpt-6-astra")}))
                calls = [call.args for call in screen.addnstr.call_args_list if call.args[0] == 3]
                if no_color or not available:
                    self.assertTrue(all(args[4] in (0, 1) for args in calls))
                    curses.init_pair.assert_not_called()
                else:
                    self.assertTrue(any(args[1] == 45 and args[4] & ~1 for args in calls))
                    self.assertTrue(all(call.args[0] < 8 for call in curses.init_pair.call_args_list))

    def test_wide_renderer_shows_fields_and_footer(self):
        screen = FakeScreen(width=120)
        dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(no_color=True))
        dashboard.update(_snapshot(metadata={wd_metadata.target_key(_item()): _metadata(
            client="Desktop", task="Terminal dashboard", model="gpt-5", effort="high",
            started=NOW - timedelta(minutes=10), cwd="/repo", provenance="test")
        }))
        output = "\n".join(screen.lines.values())
        self.assertIn("CLAUDE WATCHDOG", output)
        self.assertIn("Terminal dashboard", output)
        self.assertIn("MODEL / EFFORT", output)
        self.assertIn("persisted activity", output)
        self.assertIn("SOURCE    CLIENT", output)
        self.assertGreater(screen.erase_calls, 0)
        self.assertEqual(screen.clear_calls, 0)

    def test_wide_columns_keep_cell_positions_with_unicode_metadata(self):
        item = _item()
        ascii_snap = _snapshot(metadata={wd_metadata.target_key(item): _metadata(
            client="Desktop", task="Task", model="model", effort="high")})
        wide_snap = _snapshot(metadata={wd_metadata.target_key(item): _metadata(
            client="客戶", task="界面任務", model="模型", effort="高")})
        ascii_row = next(line for line in wd_dashboard.dashboard_lines(ascii_snap) if "CODEX" in line)
        wide_row = next(line for line in wd_dashboard.dashboard_lines(wide_snap) if "CODEX" in line)
        ascii_last_cell = wd_text.text_cells(ascii_row[:ascii_row.index("5s")])
        wide_last_cell = wd_text.text_cells(wide_row[:wide_row.index("5s")])
        self.assertEqual(ascii_last_cell, wide_last_cell)

    def test_narrow_renderer_stays_within_screen_and_keeps_labels(self):
        screen = FakeScreen(width=52)
        dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(no_color=True))
        dashboard.update(_snapshot())
        self.assertTrue(screen.lines)
        self.assertTrue(all(wd_text.text_cells(text) <= 51 for text in screen.lines.values()))
        output = "\n".join(screen.lines.values())
        self.assertIn("CODEX", output)
        self.assertIn("q exit", output)

    def test_compact_row_preserves_last_and_quiet_columns_for_long_task(self):
        item = _item()
        metadata = {wd_metadata.target_key(item): _metadata(task="T" * 500)}
        lines = wd_dashboard.dashboard_lines(_snapshot(metadata=metadata), width=52, height=12)
        row = next(line for line in lines if "CODEX" in line)
        self.assertIn("5s", row)
        self.assertIn("29:55", row)

    def test_countdown_advances_between_authoritative_polls(self):
        snap = _snapshot()
        later = wd_dashboard.dashboard_lines(
            replace(snap, now=snap.now + timedelta(seconds=10)),
            width=120,
            height=12,
        )
        row = next(line for line in later if "CODEX" in line)
        self.assertIn("29:45", row)

    def test_admission_notice_is_visible_for_the_poll_cycle(self):
        snap = replace(_snapshot(), admission_notice="admitted 1 new target")
        output = "\n".join(wd_dashboard.dashboard_lines(snap, width=120, height=12))
        self.assertIn("admitted 1 new target", output)

    @unittest.skipUnless(hasattr(__import__("time"), "tzset"), "requires time.tzset")
    def test_detail_timestamp_overflow_renders_unknown(self):
        import time as system_time

        item = _item()
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            system_time.tzset()
            metadata = {wd_metadata.target_key(item): _metadata(
                task="Boundary", started=datetime(1, 1, 1, tzinfo=timezone.utc))}
            state = wd_models.DashboardState(details=True)
            output = "\n".join(wd_dashboard.dashboard_lines(
                _snapshot(metadata=metadata), state, width=120, height=12))
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            system_time.tzset()
        self.assertIn("selected: started unknown", output)

    def test_renderer_survives_small_height_and_addstr_errors(self):
        screen = FakeScreen(height=4, width=30)
        original = screen.addnstr
        screen.addnstr = mock.Mock(side_effect=[original(0, 0, "", 0), RuntimeError("edge")])
        dashboard = wd_dashboard.TerminalDashboard(screen, wd_models.Config(no_color=True))
        dashboard.update(_snapshot())

    def test_curses_context_restores_terminal_and_stdout_logging_on_failure(self):
        curses = mock.Mock()
        screen = FakeScreen()
        curses.initscr.return_value = screen
        logging = __import__("logging")
        original_handlers = list(wd_models.log.handlers)
        self.addCleanup(setattr, wd_models.log, "handlers", original_handlers)
        stream_handler = logging.StreamHandler(sys.stdout)
        wd_models.log.handlers = [stream_handler]
        with mock.patch.object(wd_dashboard, "_import_curses", return_value=curses):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with wd_dashboard.dashboard_context(wd_models.Config(display="dashboard")):
                    raise RuntimeError("boom")
        curses.endwin.assert_called_once_with()
        self.assertIn(stream_handler, wd_models.log.handlers)


class ExitReportTests(unittest.TestCase):
    def test_user_idle_reset_and_quiet_checkpoints_explain_delayed_sleep(self):
        history = wd_reporting.WatchHistory()
        quiet = replace(_snapshot(), session_quiet=True, holding_count=0, user_idle=240)
        history.observe(quiet)
        history.observe(replace(quiet, now=NOW + timedelta(seconds=60), user_idle=5))
        history.observe(replace(quiet, now=NOW + timedelta(minutes=16), user_idle=100))
        text = '\n'.join(message for _, message in history.events)
        self.assertIn('User-idle timer reset', text)
        self.assertIn('1m 40s idle / 5m required', history.events[-1][1])

    def test_empty_and_ambiguous_session_reports_include_diagnostics(self):
        empty = '\n'.join(wd_reporting.exit_report_lines(wd_reporting.WatchHistory(), wd_models.Config(), 1, 'failed'))
        self.assertIn(str(wd_models.LOG_FILE), empty)
        history = wd_reporting.WatchHistory()
        row = replace(_snapshot().rows[0], agent='Ada\x1b[2J', identity_count=3, provenance='jsonl', task='x' * 10000)
        history.observe(replace(_snapshot(), rows=(row,)))
        output = '\n'.join(wd_reporting.exit_report_lines(history, wd_models.Config(), 130, 'interrupted'))
        self.assertIn(row.path, output)
        self.assertIn('agent Ada', output)
        self.assertIn('3 identities', output)
        self.assertIn('metadata jsonl', output)
        self.assertNotIn('\x1b', output)
        self.assertLess(len(output), 6000)

    def test_only_authoritative_poll_updates_history_not_ui_repaints(self):
        cfg = wd_models.Config(idle_minutes=0, user_idle_minutes=0, session_discovery='frozen', no_color=True)
        dashboard = wd_dashboard.TerminalDashboard(FakeScreen(), cfg)
        item = _item()
        with (
            mock.patch.object(wd_activity, '_last_activity_for', return_value=NOW),
            mock.patch.object(wd_metadata, 'load_dashboard_metadata', return_value={}),
        ):
            wd_app.wait_until_quiet(cfg, [item], dashboard)
        recorded = dashboard.history.snapshot
        self.assertIsNotNone(recorded)
        events = list(dashboard.history.events)
        dashboard.update(replace(recorded, now=recorded.now + timedelta(hours=1)))
        self.assertIs(dashboard.history.snapshot, recorded)
        self.assertEqual(list(dashboard.history.events), events)

    def test_dashboard_poll_builds_ancestor_index_once_across_multiple_scans(self):
        cfg = wd_models.Config(
            idle_minutes=30,
            user_idle_minutes=0,
            session_discovery="frozen",
        )
        watched = _item(
            "rollout-2026-09-09T00-00-00-synthetic-watched.jsonl"
        )
        candidates = [
            watched,
            _item("rollout-2026-09-09T00-00-00-synthetic-quiet.jsonl"),
        ]
        dashboard = mock.Mock()
        scans = 0

        def last_activity(item, now):
            nonlocal scans
            scans += 1
            return now if scans == 1 else None

        with (
            mock.patch.object(
                wd_dashboard,
                "index_display_ancestor_candidates",
                return_value={},
            ) as index_candidates,
            mock.patch.object(
                wd_activity,
                "_last_activity_for",
                side_effect=last_activity,
            ),
            mock.patch.object(
                wd_metadata,
                "load_dashboard_metadata",
                return_value={},
            ) as load_metadata,
        ):
            wd_app.wait_until_quiet(
                cfg, [watched], dashboard, display_items=candidates
            )

        self.assertEqual(scans, 2)
        index_candidates.assert_called_once_with(candidates)
        dashboard.wait.assert_called_once_with(cfg.poll_seconds)
        self.assertEqual(len(load_metadata.call_args_list), 2)
        first_links = load_metadata.call_args_list[0].kwargs["injected_links"]
        second_links = load_metadata.call_args_list[1].kwargs["injected_links"]
        self.assertIs(first_links, second_links)
        self.assertEqual(first_links, [])

    def test_dashboard_poll_loads_only_the_missing_ancestor_without_changing_guards(self):
        cfg = wd_models.Config(
            idle_minutes=0,
            user_idle_minutes=0,
            session_discovery="frozen",
        )
        root = _item(
            "rollout-2026-09-09T00-00-00-synthetic-root.jsonl"
        )
        leader = _item(
            "rollout-2026-09-09T00-00-00-synthetic-leader.jsonl"
        )
        child = _item(
            "rollout-2026-09-09T00-00-00-synthetic-child.jsonl"
        )
        unrelated = [
            _item(
                "rollout-2026-09-09T00-00-00-"
                f"synthetic-unrelated-{index:04d}.jsonl"
            )
            for index in range(400)
        ]
        candidates = [*unrelated, root, leader, child]
        watched = [root, child]
        watched_metadata = {
            wd_metadata.target_key(root): _metadata(
                task="Root",
                session_id="synthetic-root",
                lineage_namespace="synthetic-lineage",
            ),
            wd_metadata.target_key(child): _metadata(
                task="Child",
                session_id="synthetic-child",
                parent_session_id="synthetic-leader",
                lineage_namespace="synthetic-lineage",
            ),
        }
        leader_metadata = {
            wd_metadata.target_key(leader): _metadata(
                task="Leader",
                session_id="synthetic-leader",
                parent_session_id="synthetic-root",
                lineage_namespace="synthetic-lineage",
            )
        }
        dashboard = mock.Mock()

        def metadata_for(items, task_label, **kwargs):
            loaded = list(items)
            if loaded == watched:
                self.assertIn("injected_links", kwargs)
                return watched_metadata
            self.assertEqual(kwargs, {})
            if loaded == [leader]:
                return leader_metadata
            self.fail(f"unexpected metadata load: {loaded!r}")

        with (
            mock.patch.object(wd_activity, "_last_activity_for", return_value=NOW),
            mock.patch.object(
                wd_metadata, "load_dashboard_metadata", side_effect=metadata_for
            ) as load_metadata,
        ):
            wd_app.wait_until_quiet(
                cfg, watched, dashboard, display_items=candidates
            )

        self.assertEqual(
            load_metadata.call_args_list,
            [
                mock.call(watched, cfg.task_label, injected_links=mock.ANY),
                mock.call([leader], cfg.task_label),
            ],
        )
        snapshot = dashboard.update.call_args.args[0]
        visible = wd_dashboard.visible_dashboard_rows(
            snapshot.rows,
            wd_models.DashboardState(sort="title"),
            display_rows=snapshot.display_rows,
        )
        self.assertEqual([row.task for row in visible], ["Root", "Leader", "Child"])
        self.assertEqual((snapshot.watched_count, snapshot.holding_count), (2, 0))
        self.assertTrue(snapshot.session_quiet)
        self.assertEqual(snapshot.user_idle, snapshot.user_idle_required)
        dashboard.wait.assert_not_called()

    def test_dashboard_poll_caps_ancestor_loads_when_metadata_is_invalid(self):
        cfg = wd_models.Config(
            idle_minutes=0,
            user_idle_minutes=0,
            session_discovery="frozen",
        )
        watched = [
            _item(
                "rollout-2026-09-09T00-00-00-"
                f"synthetic-child-{index:02d}.jsonl"
            )
            for index in range(5)
        ]
        parents = [
            _item(
                "rollout-2026-09-09T00-00-00-"
                f"synthetic-parent-{index:02d}.jsonl"
            )
            for index in range(5)
        ]
        watched_metadata = {
            wd_metadata.target_key(item): _metadata(
                task=f"Child {index}",
                session_id=f"synthetic-child-{index:02d}",
                parent_session_id=f"synthetic-parent-{index:02d}",
                lineage_namespace="synthetic-lineage",
            )
            for index, item in enumerate(watched)
        }
        dashboard = mock.Mock()

        def metadata_for(items, task_label, **kwargs):
            loaded = list(items)
            if loaded == watched:
                self.assertIn("injected_links", kwargs)
                return watched_metadata
            self.assertEqual(kwargs, {})
            return {
                wd_metadata.target_key(item): wd_models.SessionMetadata()
                for item in loaded
            }

        with (
            mock.patch.object(wd_models, "MAX_SYNTHESIZED_ANCESTORS", 3),
            mock.patch.object(wd_activity, "_last_activity_for", return_value=NOW),
            mock.patch.object(
                wd_metadata, "load_dashboard_metadata", side_effect=metadata_for
            ) as load_metadata,
        ):
            wd_app.wait_until_quiet(
                cfg, watched, dashboard, display_items=[*parents, *watched]
            )

        self.assertEqual(
            load_metadata.call_args_list[0],
            mock.call(watched, "prompt", injected_links=mock.ANY),
        )
        extra_items = load_metadata.call_args_list[1].args[0]
        self.assertEqual(extra_items, parents[:3])
        self.assertEqual(len(load_metadata.call_args_list), 2)
        snapshot = dashboard.update.call_args.args[0]
        self.assertEqual(snapshot.display_rows, ())
        self.assertEqual(
            (snapshot.watched_count, snapshot.holding_count, snapshot.session_quiet),
            (5, 0, True),
        )
        dashboard.wait.assert_not_called()

    def test_dashboard_live_refresh_keeps_main_call_shape_with_ancestor_candidates(self):
        cfg = wd_models.Config(
            idle_minutes=0,
            user_idle_minutes=0,
            session_discovery="live",
        )
        watched = [
            _item("rollout-2026-09-09T00-00-00-synthetic-child.jsonl")
        ]
        dashboard = mock.Mock()

        with (
            mock.patch.object(
                wd_activity, "refresh_watch_set", return_value=watched
            ) as refresh,
            mock.patch.object(wd_activity, "_last_activity_for", return_value=NOW),
            mock.patch.object(
                wd_metadata,
                "load_dashboard_metadata",
                return_value={
                    wd_metadata.target_key(watched[0]): _metadata(
                        session_id="synthetic-child",
                        lineage_namespace="synthetic-lineage",
                    )
                },
            ),
        ):
            wd_app.wait_until_quiet(
                cfg, watched, dashboard, display_items=watched
            )

        refresh.assert_called_once_with(cfg, watched, now=mock.ANY)

    def test_report_emission_respects_no_color_and_redirected_output(self):
        history = wd_reporting.WatchHistory()
        history.observe(_snapshot())
        for tty, no_color, environment in (
            (False, False, {'TERM': 'xterm-256color'}),
            (True, True, {'TERM': 'xterm-256color'}),
            (True, False, {'TERM': 'xterm-256color', 'NO_COLOR': '1'}),
            (True, False, {'TERM': 'dumb'}),
        ):
            with self.subTest(tty=tty, no_color=no_color, environment=environment):
                output = FakeTTY(tty)
                with mock.patch.object(sys, 'stdout', output), mock.patch.dict(os.environ, environment, clear=True):
                    wd_reporting.emit_exit_report(history, wd_models.Config(no_color=no_color), 130, 'interrupted')
                self.assertNotIn('\x1b', output.getvalue())

    def test_timeline_wraps_and_user_idle_uses_readable_durations(self):
        history = wd_reporting.WatchHistory()
        history.observe(replace(_snapshot(), user_idle=10057, session_quiet=True, holding_count=0,
            admission_notice='admitted ' + 'new target ' * 30))
        lines = wd_reporting.exit_report_lines(history, wd_models.Config(), 0, '', color=False)
        output = '\n'.join(lines)
        timeline = output.split('RECENT TIMELINE')[1].split('FINAL SESSIONS')[0]
        self.assertTrue(all(len(line) <= 100 for line in timeline.splitlines()))
        self.assertIn('2h 47m 37s idle / 5m required', output)

    def test_history_records_quiet_gate_resumption_and_activity_countdown(self):
        history = wd_reporting.WatchHistory()
        first = _snapshot()
        history.observe(first)
        quiet = replace(first, now=NOW + timedelta(minutes=30),
            session_quiet=True, holding_count=0, user_idle=30,
            rows=tuple(replace(row, holding=False) for row in first.rows))
        history.observe(quiet)
        history.observe(replace(quiet, now=quiet.now + timedelta(seconds=60), user_idle=300))
        history.observe(replace(first, now=quiet.now + timedelta(seconds=120)))
        text = '\n'.join(message for _, message in history.events)
        self.assertIn('All session guards quiet', text)
        self.assertIn('User-idle gate satisfied', text)
        self.assertIn('Recorded activity resumed', text)
        self.assertNotIn('tasks completed', text)

    def test_history_is_bounded_and_steady_polls_get_checkpoints(self):
        history = wd_reporting.WatchHistory()
        first = _snapshot()
        history.observe(first)
        history.observe(replace(first, now=NOW + timedelta(minutes=1)))
        self.assertEqual(len(history.events), 1)
        history.observe(replace(first, now=NOW + timedelta(minutes=15)))
        self.assertEqual(len(history.events), 2)
        for index in range(400):
            history.observe(replace(first, now=NOW + timedelta(minutes=16, seconds=index),
                admission_notice=f'admitted {index}'))
        self.assertLessEqual(len(history.events), 256)
        late = replace(first, now=NOW + timedelta(hours=8))
        history.observe(late)
        self.assertTrue(all(at >= late.now - timedelta(hours=6) for at, _ in history.events))

    def test_report_contains_every_final_session_and_no_interactive_footer(self):
        items = [_item('a'), _item('b')]
        metadata = {wd_metadata.target_key(item): _metadata(task=title, client='Codex Desktop', model='gpt-6-astra')
            for item, title in zip(items, ['First task', 'Second task\x1b[2J'])}
        history = wd_reporting.WatchHistory()
        history.observe(_snapshot(items, metadata))
        plain = '\n'.join(wd_reporting.exit_report_lines(history, wd_models.Config(), 130, 'interrupted', color=False))
        self.assertIn('CLAUDE WATCHDOG — RUN REPORT', plain)
        self.assertIn('STOPPED', plain)
        self.assertIn('FINAL SESSIONS', plain)
        self.assertIn('First task', plain)
        self.assertIn('Second task', plain)
        self.assertIn('Quiet deadline', plain)
        self.assertIn('Last recorded activity', plain)
        self.assertNotIn('q exit', plain)
        self.assertNotIn('\x1b', plain)
        self.assertNotIn('sleeping Mac', plain)
        colored = '\n'.join(wd_reporting.exit_report_lines(history, wd_models.Config(), 0, '', color=True))
        self.assertIn('\x1b[', colored)
        self.assertNotIn('\x1b', __import__('re').sub(r'\x1b\[[0-9;]*m', '', colored))

    def test_report_never_claims_sleep_or_completion_from_missing_activity(self):
        history = wd_reporting.WatchHistory()
        empty_activity = replace(_snapshot(), rows=(), holding_count=0, session_quiet=True)
        history.observe(empty_activity)
        output = '\n'.join(wd_reporting.exit_report_lines(history, wd_models.Config(dry_run=True), 0, '', color=False))
        self.assertIn('DRY RUN', output)
        self.assertIn('No usable recorded activity', output)
        self.assertNotIn('tasks completed', output)
        self.assertNotIn('slept', output.lower())


class DashboardLifecycleTests(unittest.TestCase):
    def test_cleanup_failures_skip_sleep_and_report_error(self):
        for failure in ('endwin', 'caffeinate'):
            with self.subTest(failure=failure):
                cfg = wd_models.Config(display='dashboard', dry_run=True)
                curses = mock.Mock()
                curses.initscr.return_value = FakeScreen()
                item, process, patches = self._main_patches(cfg, curses)
                if failure == 'endwin':
                    curses.endwin.side_effect = OSError('restore failed')
                else:
                    process.terminate.side_effect = PermissionError('release denied')
                output = io.StringIO()
                with ExitStack() as stack:
                    mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
                    stack.enter_context(mock.patch.object(sys, 'stdout', output))
                    self.assertEqual(wd_app.main([]), 1)
                mocks['force_sleep'].assert_not_called()
                self.assertIn('ERROR', output.getvalue())
                self.assertNotIn('SLEEP READY', output.getvalue())
                process.terminate.assert_called_once()

    def test_failed_restore_during_initialization_cannot_fall_back_to_sleep(self):
        cfg = wd_models.Config(display='auto', dry_run=True)
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        curses.noecho.side_effect = OSError('init failed')
        curses.endwin.side_effect = OSError('restore failed')
        item, process, patches = self._main_patches(cfg, curses)
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
            self.assertEqual(wd_app.main([]), 1)
        mocks['block_sleep'].assert_not_called()
        mocks['force_sleep'].assert_not_called()

    def test_exit_report_is_flushed_after_terminal_restore_and_before_sleep(self):
        cfg = wd_models.Config(display='dashboard', dry_run=True)
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        observed = []
        output = io.StringIO()
        output.flush = lambda: observed.append('flush')
        def poll(cfg, watch_set, dashboard, display_items):
            dashboard.history.observe(_snapshot())
        item, process, patches = self._main_patches(cfg, curses,
            wait_until_quiet=mock.patch.object(wd_app, 'wait_until_quiet', side_effect=poll))
        curses.endwin.side_effect = lambda: observed.append('restored')
        process.terminate.side_effect = lambda: observed.append('released')
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
            stack.enter_context(mock.patch.object(sys, 'stdout', output))
            def sleep(dry):
                self.assertIn('RUN REPORT', output.getvalue())
                self.assertEqual(observed[:3], ['restored', 'released', 'flush'])
            mocks['force_sleep'].side_effect = sleep
            self.assertEqual(wd_app.main([]), 0)
        mocks['force_sleep'].assert_called_once_with(True)

    def test_report_output_failure_cannot_skip_cleanup_or_request_sleep(self):
        cfg = wd_models.Config(display='dashboard', dry_run=True)
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        output = mock.Mock()
        output.isatty.return_value = False
        output.write.side_effect = OSError('output unavailable')
        def poll(cfg, watch_set, dashboard, display_items):
            dashboard.history.observe(_snapshot())
        item, process, patches = self._main_patches(cfg, curses,
            wait_until_quiet=mock.patch.object(wd_app, 'wait_until_quiet', side_effect=poll))
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
            stack.enter_context(mock.patch.object(sys, 'stdout', output))
            self.assertEqual(wd_app.main([]), 1)
        process.terminate.assert_called_once()
        curses.endwin.assert_called_once()
        mocks['force_sleep'].assert_not_called()

    def _main_patches(self, cfg, curses, **extra):
        item = _item()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        patches = {
            "parse_args": mock.patch.object(wd_config, "parse_args", return_value=cfg),
            "setup_logging": mock.patch.object(wd_app, "setup_logging"),
            "activity_files": mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            "select_watch_set": mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            "resolve_display": mock.patch.object(wd_dashboard, "resolve_display", return_value="dashboard"),
            "_import_curses": mock.patch.object(wd_dashboard, "_import_curses", return_value=curses),
            "block_sleep": mock.patch.object(wd_power, "block_sleep", return_value=process),
            "wait_until_quiet": mock.patch.object(wd_app, "wait_until_quiet"),
            "force_sleep": mock.patch.object(wd_power, "force_sleep"),
        }
        patches.update(extra)
        return item, process, patches

    def test_auto_falls_back_when_lazy_curses_import_fails(self):
        cfg = wd_models.Config(display="auto", dry_run=True)
        item = _item()
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_dashboard, "resolve_display", return_value="dashboard"),
            mock.patch.object(wd_dashboard, "_import_curses", side_effect=ImportError("no curses")),
            mock.patch.object(wd_power, "block_sleep", return_value=process),
            mock.patch.object(wd_app, "wait_until_quiet") as wait,
            mock.patch.object(wd_power, "force_sleep"),
        ):
            self.assertEqual(wd_app.main([]), 0)
        wait.assert_called_once_with(cfg, [item])

    def test_explicit_dashboard_import_failure_never_starts_hold(self):
        cfg = wd_models.Config(display="dashboard", dry_run=True)
        item = _item()
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_dashboard, "resolve_display", return_value="dashboard"),
            mock.patch.object(wd_dashboard, "_import_curses", side_effect=ImportError("no curses")),
            mock.patch.object(wd_power, "block_sleep") as block,
            mock.patch.object(wd_app, "wait_until_quiet") as wait,
            mock.patch.object(wd_power, "force_sleep") as sleep,
        ):
            self.assertEqual(wd_app.main([]), 1)
        block.assert_not_called()
        wait.assert_not_called()
        sleep.assert_not_called()

    def test_main_passes_yielded_dashboard_to_watch_loop(self):
        cfg = wd_models.Config(display="dashboard", dry_run=True)
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        item, process, patches = self._main_patches(cfg, curses)
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
            self.assertEqual(wd_app.main([]), 0)
        args = mocks["wait_until_quiet"].call_args.args
        self.assertEqual(args[:2], (cfg, [item]))
        self.assertIsInstance(args[2], wd_dashboard.TerminalDashboard)
        curses.endwin.assert_called_once_with()
        process.terminate.assert_called_once_with()

    def test_main_gives_dashboard_all_candidates_without_watching_quiet_one(self):
        cfg = wd_models.Config(display="dashboard", dry_run=True)
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        watched, process, patches = self._main_patches(cfg, curses)
        quiet = _item("synthetic-quiet.jsonl")
        patches["activity_files"] = mock.patch.object(
            wd_activity, "activity_files", return_value=[watched, quiet]
        )

        with ExitStack() as stack:
            mocks = {
                name: stack.enter_context(patch)
                for name, patch in patches.items()
            }
            self.assertEqual(wd_app.main([]), 0)

        args = mocks["wait_until_quiet"].call_args.args
        self.assertEqual(args[:2], (cfg, [watched]))
        self.assertEqual(args[3], [watched, quiet])
        process.terminate.assert_called_once_with()

    def test_block_sleep_interrupt_restores_dashboard(self):
        cfg = wd_models.Config(display="dashboard")
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        item, process, patches = self._main_patches(
            cfg, curses,
            block_sleep=mock.patch.object(wd_power, "block_sleep", side_effect=KeyboardInterrupt),
        )
        with ExitStack() as stack:
            for patch in patches.values():
                stack.enter_context(patch)
            self.assertEqual(wd_app.main([]), 130)
        curses.endwin.assert_called_once_with()

    def test_cleanup_interrupt_occurs_after_dashboard_restoration(self):
        cfg = wd_models.Config(display="dashboard")
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        restored = []

        def interrupted_cleanup(process):
            restored.append(curses.endwin.called)
            raise KeyboardInterrupt

        item, process, patches = self._main_patches(
            cfg, curses,
            _stop_caffeinate=mock.patch.object(wd_power, "_stop_caffeinate", side_effect=interrupted_cleanup),
        )
        with ExitStack() as stack:
            for patch in patches.values():
                stack.enter_context(patch)
            self.assertEqual(wd_app.main([]), 130)
        self.assertGreaterEqual(len(restored), 1)
        self.assertTrue(all(restored))

    def test_dashboard_initialization_interrupt_exits_without_starting_hold(self):
        cfg = wd_models.Config(display="dashboard")
        item = _item()
        with (
            mock.patch.object(wd_config, "parse_args", return_value=cfg),
            mock.patch.object(wd_app, "setup_logging"),
            mock.patch.object(wd_activity, "activity_files", return_value=[item]),
            mock.patch.object(wd_activity, "select_watch_set", return_value=[item]),
            mock.patch.object(wd_dashboard, "resolve_display", return_value="dashboard"),
            mock.patch.object(wd_dashboard, "_import_curses", side_effect=KeyboardInterrupt),
            mock.patch.object(wd_power, "block_sleep") as block,
        ):
            self.assertEqual(wd_app.main([]), 130)
        block.assert_not_called()

    def test_dashboard_exit_interrupt_releases_existing_hold_without_rerun(self):
        cfg = wd_models.Config(display="auto")
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        curses.endwin.side_effect = KeyboardInterrupt
        item, process, patches = self._main_patches(cfg, curses)
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
            self.assertEqual(wd_app.main([]), 130)
        mocks["block_sleep"].assert_called_once_with()
        process.terminate.assert_called_once()

    def test_unexpected_display_failure_restores_and_aborts_without_sleep(self):
        cfg = wd_models.Config(display="dashboard")
        curses = mock.Mock()
        curses.initscr.return_value = FakeScreen()
        item, process, patches = self._main_patches(
            cfg, curses,
            wait_until_quiet=mock.patch.object(wd_app, "wait_until_quiet", side_effect=RuntimeError("render broke")),
        )
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch) for name, patch in patches.items()}
            with self.assertLogs(wd_models.log, level="ERROR") as captured:
                self.assertEqual(wd_app.main([]), 1)
        curses.endwin.assert_called_once_with()
        process.terminate.assert_called_once_with()
        mocks["block_sleep"].assert_called_once_with()
        mocks["force_sleep"].assert_not_called()
        self.assertTrue(any("render broke" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
