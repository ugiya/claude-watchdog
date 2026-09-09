#!/usr/bin/env python3
"""Regression tests for dashboard session ancestry and tree navigation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path


from claude_watchdog import models as wd_models
from claude_watchdog import metadata as wd_metadata
from claude_watchdog import dashboard as wd_dashboard


def _row(
    name: str, session_id: str, parent_session_id: str = "unknown",
    lineage_namespace: str = "unknown",
):
    return wd_models.SessionRow(
        key=("codex", f"/{name}.jsonl"), source="codex", client="codex_exec",
        task=name, model="gpt", effort="high", started=None,
        last_event=datetime(2026, 9, 6, tzinfo=timezone.utc), quiet_remaining=60,
        path=f"/{name}.jsonl", identity_count=0, provenance="jsonl",
        holding=True, session_id=session_id, parent_session_id=parent_session_id,
        lineage_namespace=lineage_namespace,
    )


def _legacy_dashboard_parent_keys(rows):
    """Oracle copied from the pre-extraction dashboard resolver."""
    row_list = list(rows)
    row_keys = {}
    for row in row_list:
        row_keys.setdefault(row.key, []).append(row)
    identities = {}
    for row in row_list:
        if row.session_id != wd_models.UNKNOWN:
            identities.setdefault(
                (row.source, row.lineage_namespace, row.session_id), []
            ).append(row)
    parents = {}
    for row in row_list:
        matches = identities.get(
            (row.source, row.lineage_namespace, row.parent_session_id), []
        )
        if len(matches) == 1 and matches[0].key != row.key:
            parents[row.key] = matches[0].key
        elif (
            row.parent_session_id == wd_models.UNKNOWN
            and row.external_parent_key is not None
        ):
            external_matches = row_keys.get(row.external_parent_key, [])
            if len(external_matches) == 1 and external_matches[0].key != row.key:
                parents[row.key] = external_matches[0].key

    cyclic = set()
    for row in row_list:
        path, positions = [], {}
        current = row.key
        while current in parents and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        if current in positions:
            cyclic.update(path[positions[current]:])
    for key in cyclic:
        parents.pop(key, None)
    return parents

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
            result = wd_metadata.jsonl_metadata(wd_models.ActivityFile(path, "codex"))
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

            claude_item = wd_models.ActivityFile(claude_path, "claude")
            codex_item = wd_models.ActivityFile(codex_path, "codex")
            claude = wd_metadata.jsonl_metadata(claude_item)
            codex = wd_metadata.jsonl_metadata(codex_item)
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now, wd_models.Config(), [claude_item, codex_item],
                [(claude_item, now), (codex_item, now)], 0, 10,
                {
                    wd_metadata.target_key(claude_item): claude,
                    wd_metadata.target_key(codex_item): codex,
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
            item = wd_models.ActivityFile(database_path, "opencode", identities=frozenset({"root"}))
            metadata = wd_metadata.opencode_metadata(item)
            now = datetime(2026, 9, 6, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now, wd_models.Config(), [item], [(item, now - timedelta(seconds=5))],
                0, 10, {wd_metadata.target_key(item): metadata},
            )

        visible = wd_dashboard.visible_dashboard_rows(
            snapshot.rows, wd_models.DashboardState(sort="title")
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
            list(wd_dashboard.dashboard_tree_prefixes(visible).values()),
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
        state = wd_models.DashboardState(sort="title")

        visible = wd_dashboard.visible_dashboard_rows(rows, state)
        prefixes = wd_dashboard.dashboard_tree_prefixes(visible)

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
        state = wd_models.DashboardState(sort="title")

        visible = wd_dashboard.visible_dashboard_rows([cycle_b, orphan, cycle_a], state)

        self.assertEqual([row.task for row in visible], ["Cycle A", "Cycle B", "Orphan"])
        self.assertEqual(
            wd_dashboard.dashboard_tree_prefixes(visible),
            {cycle_a.key: "", cycle_b.key: "", orphan.key: ""},
        )

    def test_t_toggles_between_tree_and_flat_sort(self):
        child = _row("Alpha child", "child", "parent")
        parent = _row("Zulu parent", "parent")
        state = wd_models.DashboardState(sort="title")
        self.assertEqual(
            [row.task for row in wd_dashboard.visible_dashboard_rows([parent, child], state)],
            ["Zulu parent", "Alpha child"],
        )

        wd_dashboard.handle_dashboard_key(state, "t", 2)

        self.assertFalse(state.tree)
        self.assertEqual(
            [row.task for row in wd_dashboard.visible_dashboard_rows([parent, child], state)],
            ["Alpha child", "Zulu parent"],
        )

    def test_same_session_ids_in_different_namespaces_do_not_cross_link(self):
        parent = _row("Parent A", "shared", lineage_namespace="database-a")
        child = _row("Child B", "child", "shared", lineage_namespace="database-b")

        visible = wd_dashboard.visible_dashboard_rows(
            [child, parent], wd_models.DashboardState(sort="title")
        )

        self.assertEqual([row.task for row in visible], ["Child B", "Parent A"])
        self.assertEqual(
            wd_dashboard.dashboard_tree_prefixes(visible),
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

        visible = wd_dashboard.visible_dashboard_rows(
            reversed(rows), wd_models.DashboardState(sort="title")
        )

        self.assertEqual(len(visible), 1100)
        self.assertEqual((visible[0].session_id, visible[-1].session_id),
                         ("node-0", "node-1099"))

    def test_dashboard_parent_keys_match_legacy_oracle(self):
        def row(
            source,
            name,
            session_id,
            parent_session_id=wd_models.UNKNOWN,
            *,
            key_name=None,
            external_parent_key=None,
        ):
            path = f"synthetic-{key_name or name}.jsonl"
            return replace(
                _row(name, session_id, parent_session_id, "synthetic-lineage"),
                key=(source, path),
                source=source,
                path=path,
                external_parent_key=external_parent_key,
            )

        present_external_key = ("claude", "synthetic-external-parent.jsonl")
        duplicated_external_key = ("opencode", "synthetic-duplicate.jsonl")
        blocked_external_key = ("codex", "synthetic-blocked-parent.jsonl")
        row_sets = {
            "native_identity_and_unknown_exclusion": [
                row("codex", "native-parent", "shared-parent"),
                row("codex", "native-child", "codex-child", "shared-parent"),
                row("claude", "provider-local-parent", "shared-parent"),
                row("claude", "self-parent", "self-id", "self-id"),
                row("opencode", "unknown-identity", wd_models.UNKNOWN),
                row("opencode", "unknown-child", "unknown-child"),
            ],
            "external_present_missing_and_duplicate": [
                row("claude", "external-parent", "external-parent"),
                row(
                    "claude",
                    "external-child",
                    "external-child",
                    external_parent_key=present_external_key,
                ),
                row(
                    "codex",
                    "missing-external-child",
                    "missing-external-child",
                    external_parent_key=("codex", "synthetic-missing.jsonl"),
                ),
                row(
                    "opencode",
                    "duplicate-first",
                    "duplicate-first",
                    key_name="duplicate",
                ),
                row(
                    "opencode",
                    "duplicate-second",
                    "duplicate-second",
                    key_name="duplicate",
                ),
                row(
                    "opencode",
                    "ambiguous-external-child",
                    "ambiguous-external-child",
                    external_parent_key=duplicated_external_key,
                ),
            ],
            "known_unresolvable_parent_blocks_external_fallback": [
                row("codex", "blocked-parent", "external-parent"),
                row(
                    "codex",
                    "blocked-child",
                    "blocked-child",
                    "missing-native-parent",
                    external_parent_key=blocked_external_key,
                ),
                row("claude", "other-provider", "external-parent"),
                row("opencode", "unknown-provider-row", wd_models.UNKNOWN),
            ],
        }

        for name, rows in row_sets.items():
            with self.subTest(name=name):
                # The copied oracle is equivalent only while row.source == key[0].
                self.assertTrue(all(row.source == row.key[0] for row in rows))
                self.assertEqual(
                    wd_dashboard._dashboard_parent_keys(rows),
                    _legacy_dashboard_parent_keys(rows),
                )

    def test_dashboard_row_constructors_preserve_source_key_invariant(self):
        now = datetime(2026, 9, 9, tzinfo=timezone.utc)
        items = [
            wd_models.ActivityFile(Path("synthetic-codex.jsonl"), "codex"),
            wd_models.ActivityFile(Path("synthetic-claude.jsonl"), "claude"),
            wd_models.ActivityFile(Path("synthetic-opencode.db"), "opencode"),
        ]
        metadata = {
            wd_metadata.target_key(items[2]): wd_models.SessionMetadata(
                lineage_namespace="synthetic-lineage",
                children=(wd_models.SessionChildMetadata("synthetic-child"),),
            )
        }

        snapshot = wd_dashboard.make_dashboard_snapshot(
            now,
            wd_models.Config(),
            items,
            [(item, now) for item in items],
            0,
            10,
            metadata,
        )
        expanded = wd_dashboard._expanded_dashboard_rows(snapshot.rows)

        self.assertEqual(
            [row.source for row in snapshot.rows],
            [row.key[0] for row in snapshot.rows],
        )
        self.assertGreater(len(expanded), len(snapshot.rows))
        self.assertEqual(
            [row.source for row in expanded],
            [row.key[0] for row in expanded],
        )


class TreeFrameTests(unittest.TestCase):
    def test_nested_prefix_spacing_and_group_timing_survive_full_frame_render(self):
        root = wd_models.SessionChildMetadata(
            "root", task="Build UI", model="muse", effort="high", agent="build"
        )
        child = wd_models.SessionChildMetadata(
            "child", "root", "Review UI", "muse", "medium", "explore"
        )
        item = wd_models.ActivityFile(Path("/tmp/opencode.db"), "opencode",
                                     identities=frozenset({"root"}))
        now = datetime(2026, 9, 6, tzinfo=timezone.utc)
        metadata = wd_models.SessionMetadata(
            client="OpenCode", task="2 sessions", model="mixed", effort="mixed",
            provenance="opencode-state", lineage_namespace=str(item.path),
            children=(root, child),
        )
        snapshot = wd_dashboard.make_dashboard_snapshot(
            now, wd_models.Config(), [item], [(item, now - timedelta(seconds=5))],
            0, 10, {wd_metadata.target_key(item): metadata},
        )
        screen = FakeScreen()

        wd_dashboard.TerminalDashboard(
            screen, wd_models.Config(no_color=True)
        ).update(snapshot)

        self.assertIn("└─ Build UI", screen.lines[4])
        self.assertIn("   └─ Review UI", screen.lines[5])
        self.assertTrue(screen.lines[4].rstrip().endswith("group      group"))
        self.assertTrue(screen.lines[5].rstrip().endswith("group      group"))


if __name__ == "__main__":
    unittest.main()
