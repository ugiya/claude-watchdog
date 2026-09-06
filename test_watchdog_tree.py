#!/usr/bin/env python3
"""Regression tests for dashboard session ancestry and tree navigation."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_target():
    target = Path(__file__).with_name("claude-watchdog")
    loader = importlib.machinery.SourceFileLoader("watchdog_tree_target", str(target))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


watchdog = _load_target()


def _row(
    name: str, session_id: str, parent_session_id: str = "unknown",
    lineage_namespace: str = "unknown",
):
    return watchdog.SessionRow(
        key=("codex", f"/{name}.jsonl"), source="codex", client="codex_exec",
        task=name, model="gpt", effort="high", started=None,
        last_event=datetime(2026, 9, 6, tzinfo=timezone.utc), quiet_remaining=60,
        path=f"/{name}.jsonl", identity_count=0, provenance="jsonl",
        holding=True, session_id=session_id, parent_session_id=parent_session_id,
        lineage_namespace=lineage_namespace,
    )


class FakeScreen:
    def __init__(self, height=20, width=180):
        self.height = height
        self.width = width
        self.lines = {}

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.lines.clear()

    def addnstr(self, y, x, text, count, *attrs):
        self.lines[y] = text[:count]

    def refresh(self):
        pass


class AncestryMetadataTests(unittest.TestCase):
    def test_inherited_parent_session_meta_does_not_replace_child_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "child.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": "child", "agent_nickname": "Singer", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}}},
                {"type": "session_meta", "payload": {"id": "root", "source": "cli", "agent_nickname": "Parent"}},
                {"type": "turn_context", "payload": {"model": "current-model", "reasoning_effort": "medium"}},
            ]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
            result = watchdog.jsonl_metadata(watchdog.ActivityFile(path, "codex"))
        self.assertEqual(result.session_id, "child")
        self.assertEqual(result.parent_session_id, "root")
        self.assertEqual(result.agent, "Singer")
        self.assertEqual(result.model, "current-model")

    def test_explicit_claude_and_codex_parentage_reaches_dashboard_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_id = "parent-session"
            claude_path = root / parent_id / "subagents" / "agent-reviewer.jsonl"
            claude_path.parent.mkdir(parents=True)
            claude_path.write_text(json.dumps({
                "type": "user", "agentId": "reviewer", "entrypoint": "cli",
            }) + "\n")
            codex_path = root / "rollout-child.jsonl"
            codex_path.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "codex-child",
                    "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": "codex-parent",
                    }}},
                },
            }) + "\n")

            claude_item = watchdog.ActivityFile(claude_path, "claude")
            codex_item = watchdog.ActivityFile(codex_path, "codex")
            claude = watchdog.jsonl_metadata(claude_item)
            codex = watchdog.jsonl_metadata(codex_item)
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            snapshot = watchdog.make_dashboard_snapshot(
                now, watchdog.Config(), [claude_item, codex_item],
                [(claude_item, now), (codex_item, now)], 0, 10,
                {
                    watchdog.target_key(claude_item): claude,
                    watchdog.target_key(codex_item): codex,
                },
            )

        self.assertEqual((claude.session_id, claude.parent_session_id),
                         ("reviewer", parent_id))
        self.assertEqual((codex.session_id, codex.parent_session_id),
                         ("codex-child", "codex-parent"))
        self.assertEqual(
            [(row.session_id, row.parent_session_id) for row in snapshot.rows],
            [("reviewer", parent_id), ("codex-child", "codex-parent")],
        )

    def test_opencode_sessions_expand_below_one_activity_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "opencode.db"
            database = sqlite3.connect(database_path)
            with database:
                database.execute(
                    "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, "
                    "title TEXT, directory TEXT, time_created INTEGER, "
                    "time_updated INTEGER, model TEXT, agent TEXT)"
                )
                database.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, '/repo', ?, ?, ?, ?)",
                    [
                        ("root", None, "Build UI", 1000, 2000,
                         json.dumps({"id": "muse", "variant": "high"}), "build"),
                        ("child", "root", "Review UI", 1100, 2100,
                         json.dumps({"id": "muse", "variant": "medium"}), "explore"),
                        ("outside", None, "Unrelated", 1200, 2200,
                         json.dumps({"id": "other"}), "build"),
                    ],
                )
            database.close()
            item = watchdog.ActivityFile(database_path, "opencode", identities=frozenset({"root"}))
            metadata = watchdog.opencode_metadata(item)
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            snapshot = watchdog.make_dashboard_snapshot(
                now, watchdog.Config(), [item], [(item, now - timedelta(seconds=5))],
                0, 10, {watchdog.target_key(item): metadata},
            )

        visible = watchdog.visible_dashboard_rows(
            snapshot.rows, watchdog.DashboardState(sort="title")
        )
        self.assertEqual(snapshot.watched_count, 1)
        self.assertEqual([row.task for row in visible], [metadata.task, "Build UI", "Review UI"])
        self.assertEqual([row.display_only for row in visible], [False, True, True])
        self.assertEqual(
            [(row.model, row.effort, row.agent) for row in visible[1:]],
            [("muse", "high", "build"), ("muse", "medium", "explore")],
        )
        self.assertEqual([row.last_event for row in visible[1:]], [None, None])
        self.assertNotIn("Unrelated", [row.task for row in visible])
        self.assertEqual(
            list(watchdog.dashboard_tree_prefixes(visible).values()),
            ["", "└─ ", "   └─ "],
        )


class TreePresentationTests(unittest.TestCase):
    def test_tree_order_keeps_descendants_below_parent_with_pstree_prefixes(self):
        rows = [
            _row("Grandchild", "grand", "child"),
            _row("Sibling", "sibling", "root"),
            _row("Other root", "other"),
            _row("Child", "child", "root"),
            _row("Root", "root"),
        ]
        state = watchdog.DashboardState(sort="title")

        visible = watchdog.visible_dashboard_rows(rows, state)
        prefixes = watchdog.dashboard_tree_prefixes(visible)

        self.assertEqual(
            [row.task for row in visible],
            ["Other root", "Root", "Child", "Grandchild", "Sibling"],
        )
        self.assertEqual(prefixes, {
            rows[2].key: "",
            rows[4].key: "",
            rows[3].key: "├─ ",
            rows[0].key: "│  └─ ",
            rows[1].key: "└─ ",
        })

    def test_missing_parent_and_cycle_remain_visible_as_safe_roots(self):
        orphan = _row("Orphan", "orphan", "filtered-parent")
        cycle_a = _row("Cycle A", "a", "b")
        cycle_b = _row("Cycle B", "b", "a")
        state = watchdog.DashboardState(sort="title")

        visible = watchdog.visible_dashboard_rows([cycle_b, orphan, cycle_a], state)

        self.assertEqual([row.task for row in visible], ["Cycle A", "Cycle B", "Orphan"])
        self.assertEqual(
            watchdog.dashboard_tree_prefixes(visible),
            {cycle_a.key: "", cycle_b.key: "", orphan.key: ""},
        )

    def test_t_toggles_between_tree_and_flat_sort(self):
        child = _row("Alpha child", "child", "parent")
        parent = _row("Zulu parent", "parent")
        state = watchdog.DashboardState(sort="title")
        self.assertEqual(
            [row.task for row in watchdog.visible_dashboard_rows([parent, child], state)],
            ["Zulu parent", "Alpha child"],
        )

        watchdog.handle_dashboard_key(state, "t", 2)

        self.assertFalse(state.tree)
        self.assertEqual(
            [row.task for row in watchdog.visible_dashboard_rows([parent, child], state)],
            ["Alpha child", "Zulu parent"],
        )

    def test_same_session_ids_in_different_namespaces_do_not_cross_link(self):
        parent = _row("Parent A", "shared", lineage_namespace="database-a")
        child = _row("Child B", "child", "shared", lineage_namespace="database-b")

        visible = watchdog.visible_dashboard_rows(
            [child, parent], watchdog.DashboardState(sort="title")
        )

        self.assertEqual([row.task for row in visible], ["Child B", "Parent A"])
        self.assertEqual(
            watchdog.dashboard_tree_prefixes(visible),
            {child.key: "", parent.key: ""},
        )

    def test_deep_tree_ordering_does_not_use_python_recursion(self):
        rows = [
            _row(
                f"Node {index:04d}", f"node-{index}",
                "unknown" if index == 0 else f"node-{index - 1}",
            )
            for index in range(1100)
        ]

        visible = watchdog.visible_dashboard_rows(
            reversed(rows), watchdog.DashboardState(sort="title")
        )

        self.assertEqual(len(visible), 1100)
        self.assertEqual((visible[0].session_id, visible[-1].session_id),
                         ("node-0", "node-1099"))


class TreeFrameTests(unittest.TestCase):
    def test_nested_prefix_spacing_and_group_timing_survive_full_frame_render(self):
        root = watchdog.SessionChildMetadata(
            "root", task="Build UI", model="muse", effort="high", agent="build"
        )
        child = watchdog.SessionChildMetadata(
            "child", "root", "Review UI", "muse", "medium", "explore"
        )
        item = watchdog.ActivityFile(Path("/tmp/opencode.db"), "opencode",
                                     identities=frozenset({"root"}))
        now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        metadata = watchdog.SessionMetadata(
            client="OpenCode", task="2 sessions", model="mixed", effort="mixed",
            provenance="opencode-state", lineage_namespace=str(item.path),
            children=(root, child),
        )
        snapshot = watchdog.make_dashboard_snapshot(
            now, watchdog.Config(), [item], [(item, now - timedelta(seconds=5))],
            0, 10, {watchdog.target_key(item): metadata},
        )
        screen = FakeScreen()

        watchdog.TerminalDashboard(
            screen, watchdog.Config(no_color=True)
        ).update(snapshot)

        self.assertIn("└─ Build UI", screen.lines[4])
        self.assertIn("   └─ Review UI", screen.lines[5])
        self.assertTrue(screen.lines[4].rstrip().endswith("group      group"))
        self.assertTrue(screen.lines[5].rstrip().endswith("group      group"))


if __name__ == "__main__":
    unittest.main()
