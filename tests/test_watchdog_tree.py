#!/usr/bin/env python3
"""Regression tests for dashboard session ancestry and tree navigation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path
from unittest import mock


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


def _codex_rollout(
    root: Path,
    project: Path,
    name: str,
    session_id: str,
    *,
    source: object = "exec",
) -> wd_models.ActivityFile:
    path = root / f"rollout-{name}.jsonl"
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "originator": "codex_exec",
            "source": source,
            "cwd": str(project),
        },
    }) + "\n", encoding="utf-8")
    return wd_models.ActivityFile(path, "codex")


def _write_omx_tracking(
    project: Path,
    leader_thread_id: str,
    threads: dict[str, object],
    *,
    schema_version: object = 1,
) -> Path:
    path = project / ".omx" / "state" / "subagent-tracking.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schemaVersion": schema_version,
        "sessions": {
            "omx-synthetic-launch": {
                "session_id": "omx-synthetic-launch",
                "leader_thread_id": leader_thread_id,
                "updated_at": "2026-09-09T08:00:00Z",
                "threads": threads,
            },
        },
    }), encoding="utf-8")
    return path


def _write_omx_tracking_document(project: Path, document: object) -> Path:
    path = project / ".omx" / "state" / "subagent-tracking.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_external_lineage_registry(
    path: Path, child_id: str, parent_id: str
) -> Path:
    path.write_text(json.dumps({
        "version": 1,
        "links": [{
            "child": {"source": "codex", "session_id": child_id},
            "parent": {"source": "codex", "session_id": parent_id},
            "evidence": "synthetic launch record",
        }],
    }), encoding="utf-8")
    return path


def _load_codex_metadata(
    items: tuple[wd_models.ActivityFile, ...],
    registry_path: Path | None = None,
) -> dict[tuple[str, str], wd_models.SessionMetadata]:
    registry = registry_path or items[0].path.parent / "missing-lineage.json"
    with (
        mock.patch.object(wd_metadata, "codex_sqlite_metadata_batch", return_value={}),
        mock.patch.object(
            wd_metadata, "external_lineage_registry_path", return_value=registry
        ),
    ):
        return wd_metadata.load_dashboard_metadata(items)


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
    def test_omx_tracking_cycle_through_external_registry_reverts_only_tracking_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            root_item = _codex_rollout(
                root, project, "root", "root-thread", source="cli"
            )
            leader = _codex_rollout(root, project, "leader", "leader-thread")
            _write_omx_tracking(
                project,
                "leader-thread",
                {
                    "root-thread": {
                        "thread_id": "root-thread",
                        "kind": "subagent",
                    }
                },
            )
            registry = _write_external_lineage_registry(
                root / "lineage.json", "leader-thread", "root-thread"
            )

            items = (root_item, leader)
            metadata = _load_codex_metadata(items, registry)
            now = datetime(2026, 9, 9, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now,
                wd_models.Config(),
                list(items),
                [(item, now) for item in items],
                0,
                10,
                metadata,
            )

        self.assertEqual(
            metadata[wd_metadata.target_key(root_item)].parent_session_id,
            wd_models.UNKNOWN,
        )
        self.assertEqual(
            metadata[wd_metadata.target_key(leader)].external_parent_key,
            wd_metadata.target_key(root_item),
        )
        self.assertEqual(
            list(wd_dashboard.dashboard_tree_prefixes(snapshot.rows).values()),
            ["", "└─ "],
        )

    def test_omx_tracking_precedes_different_external_registry_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            tracking_leader = _codex_rollout(
                root, project, "tracking-leader", "tracking-leader-thread"
            )
            registry_parent = _codex_rollout(
                root, project, "registry-parent", "registry-parent-thread"
            )
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(
                project,
                "tracking-leader-thread",
                {
                    "child-thread": {
                        "thread_id": "child-thread",
                        "kind": "subagent",
                    }
                },
            )
            registry = _write_external_lineage_registry(
                root / "lineage.json", "child-thread", "registry-parent-thread"
            )

            metadata = _load_codex_metadata(
                (tracking_leader, registry_parent, child), registry
            )
            child_metadata = metadata[wd_metadata.target_key(child)]

        self.assertEqual(
            child_metadata.parent_session_id, "tracking-leader-thread"
        )
        self.assertIsNone(child_metadata.external_parent_key)

    def test_omx_tracking_parent_with_duplicate_identity_draws_no_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            first_parent = _codex_rollout(
                root, project, "first-parent", "shared-parent-thread"
            )
            second_parent = _codex_rollout(
                root, project, "second-parent", "shared-parent-thread"
            )
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(
                project,
                "shared-parent-thread",
                {
                    "child-thread": {
                        "thread_id": "child-thread",
                        "kind": "subagent",
                    }
                },
            )

            items = (first_parent, second_parent, child)
            metadata = _load_codex_metadata(items)
            now = datetime(2026, 9, 9, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now,
                wd_models.Config(),
                list(items),
                [(item, now) for item in items],
                0,
                10,
                metadata,
            )

        self.assertEqual(
            wd_dashboard.dashboard_tree_prefixes(snapshot.rows),
            {wd_metadata.target_key(item): "" for item in items},
        )

    def test_apply_omx_tracking_lineage_skips_self_referential_candidate(self):
        child_key = ("codex", "synthetic-child")
        metadata = {
            child_key: wd_models.SessionMetadata(
                session_id="child-thread",
                tracking_parent_session_id="child-thread",
            )
        }

        result = wd_metadata.apply_omx_tracking_lineage(metadata)

        self.assertEqual(
            result[child_key].parent_session_id, wd_models.UNKNOWN
        )

    def test_omx_tracking_cycle_guard_preserves_embedded_edge_regardless_of_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            first = _codex_rollout(root, project, "first", "first-thread")
            second = _codex_rollout(root, project, "second", "second-thread")
            third = _codex_rollout(
                root,
                project,
                "third",
                "third-thread",
                source={
                    "subagent": {
                        "thread_spawn": {"parent_thread_id": "first-thread"}
                    }
                },
            )
            _write_omx_tracking_document(
                project,
                {
                    "schemaVersion": 1,
                    "sessions": {
                        "omx-synthetic-first-launch": {
                            "session_id": "omx-synthetic-first-launch",
                            "leader_thread_id": "second-thread",
                            "threads": {
                                "first-thread": {
                                    "thread_id": "first-thread",
                                    "kind": "subagent",
                                }
                            },
                        },
                        "omx-synthetic-second-launch": {
                            "session_id": "omx-synthetic-second-launch",
                            "leader_thread_id": "third-thread",
                            "threads": {
                                "second-thread": {
                                    "thread_id": "second-thread",
                                    "kind": "subagent",
                                }
                            },
                        },
                    },
                },
            )

            items = (first, second, third)
            for item_order in permutations(items):
                with self.subTest(order=tuple(item.path.name for item in item_order)):
                    metadata = _load_codex_metadata(item_order)
                    self.assertEqual(
                        {
                            metadata[wd_metadata.target_key(item)].session_id:
                            metadata[wd_metadata.target_key(item)].parent_session_id
                            for item in items
                        },
                        {
                            "first-thread": wd_models.UNKNOWN,
                            "second-thread": wd_models.UNKNOWN,
                            "third-thread": "first-thread",
                        },
                    )

            now = datetime(2026, 9, 9, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now,
                wd_models.Config(),
                list(items),
                [(item, now) for item in items],
                0,
                10,
                _load_codex_metadata(items),
            )

        self.assertEqual(
            list(wd_dashboard.dashboard_tree_prefixes(snapshot.rows).values()),
            ["", "", "└─ "],
        )

    def test_omx_tracking_cycle_guard_preserves_embedded_three_level_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            root_item = _codex_rollout(root, project, "root", "root-thread")
            leader = _codex_rollout(
                root,
                project,
                "leader",
                "leader-thread",
                source={
                    "subagent": {
                        "thread_spawn": {"parent_thread_id": "root-thread"}
                    }
                },
            )
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(
                project,
                "leader-thread",
                {
                    "root-thread": {
                        "thread_id": "root-thread",
                        "kind": "subagent",
                    },
                    "leader-thread": {
                        "thread_id": "leader-thread",
                        "kind": "leader",
                    },
                    "child-thread": {
                        "thread_id": "child-thread",
                        "kind": "subagent",
                    },
                },
            )

            items = (root_item, leader, child)
            metadata = _load_codex_metadata(items)
            now = datetime(2026, 9, 9, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now,
                wd_models.Config(),
                list(items),
                [(item, now) for item in items],
                0,
                10,
                metadata,
            )

        self.assertEqual(
            [metadata[wd_metadata.target_key(item)].parent_session_id for item in items],
            [wd_models.UNKNOWN, "root-thread", "leader-thread"],
        )
        self.assertEqual(
            list(wd_dashboard.dashboard_tree_prefixes(snapshot.rows).values()),
            ["", "└─ ", "   └─ "],
        )

    def test_omx_tracking_supports_cli_and_vscode_subagents_without_embedded_parent(self):
        for source in ("cli", "vscode"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "synthetic-project"
                project.mkdir()
                child = _codex_rollout(
                    root, project, "child", "child-thread", source=source
                )
                _write_omx_tracking(
                    project,
                    "leader-thread",
                    {
                        "child-thread": {
                            "thread_id": "child-thread",
                            "kind": "subagent",
                        }
                    },
                )

                result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

            self.assertEqual(result.parent_session_id, "leader-thread")

    def test_recursive_omx_tracking_json_preserves_rollout_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            path = project / ".omx" / "state" / "subagent-tracking.json"
            path.parent.mkdir(parents=True)
            # Python <= 3.13 raises RecursionError; 3.14 raises JSONDecodeError.
            path.write_bytes(b"[" * (64 * 1024))

            result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

        self.assertEqual(result.client, "codex_exec")
        self.assertEqual(result.cwd, str(project))
        self.assertEqual(result.session_id, "child-thread")
        self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_recursive_omx_session_json_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            path = project / ".omx" / "state" / "session.json"
            path.parent.mkdir(parents=True)
            # Python <= 3.13 raises RecursionError; 3.14 raises JSONDecodeError.
            path.write_bytes(b"[" * (64 * 1024))

            result = wd_metadata.jsonl_metadata(child)

        self.assertEqual(result.client, "codex_exec")
        self.assertEqual(result.session_id, "child-thread")

    def test_omx_tracking_reads_documents_above_metadata_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            path = _write_omx_tracking_document(
                project,
                {
                    "schemaVersion": 1,
                    "padding": "x" * wd_models.READ_CHUNK_BYTES,
                    "sessions": {
                        "omx-synthetic-launch": {
                            "session_id": "omx-synthetic-launch",
                            "leader_thread_id": "leader-thread",
                            "threads": {
                                "child-thread": {
                                    "thread_id": "child-thread",
                                    "kind": "subagent",
                                }
                            },
                        }
                    },
                },
            )
            self.assertGreater(path.stat().st_size, wd_models.READ_CHUNK_BYTES)

            result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

        self.assertEqual(result.parent_session_id, "leader-thread")

    def test_omx_tracking_rejects_document_above_byte_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            document = {
                "schemaVersion": 1,
                "sessions": {
                    "omx-synthetic-launch": {
                        "session_id": "omx-synthetic-launch",
                        "leader_thread_id": "leader-thread",
                        "threads": {
                            "child-thread": {
                                "thread_id": "child-thread",
                                "kind": "subagent",
                            }
                        },
                    }
                },
                "padding": "",
            }
            encoded_size = len(json.dumps(document).encode("utf-8"))
            document["padding"] = "x" * (
                wd_models.MAX_OMX_TRACKING_BYTES + 1 - encoded_size
            )
            path = _write_omx_tracking_document(project, document)
            self.assertEqual(
                path.stat().st_size, wd_models.MAX_OMX_TRACKING_BYTES + 1
            )

            result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

        self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_omx_tracking_self_parent_subagent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            _write_omx_tracking(
                project,
                "leader-thread",
                {
                    "leader-thread": {
                        "thread_id": "leader-thread",
                        "kind": "subagent",
                    }
                },
            )

            result = wd_metadata._omx_tracking_parent(
                "leader-thread", str(project)
            )

        self.assertIsNone(result)

    def test_omx_tracking_schema_version_requires_integer_one(self):
        for schema_version in (True, 1.0, "1"):
            with self.subTest(schema_version=schema_version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "synthetic-project"
                project.mkdir()
                child = _codex_rollout(root, project, "child", "child-thread")
                _write_omx_tracking(
                    project,
                    "leader-thread",
                    {
                        "child-thread": {
                            "thread_id": "child-thread",
                            "kind": "subagent",
                        }
                    },
                    schema_version=schema_version,
                )

                result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

            self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_omx_tracking_conflicting_parent_declarations_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            sessions = {}
            for index, leader_thread_id in enumerate(("leader-one", "leader-two")):
                launch_id = f"omx-synthetic-launch-{index}"
                sessions[launch_id] = {
                    "session_id": launch_id,
                    "leader_thread_id": leader_thread_id,
                    "threads": {
                        "child-thread": {
                            "thread_id": "child-thread",
                            "kind": "subagent",
                        }
                    },
                }
            _write_omx_tracking_document(
                project, {"schemaVersion": 1, "sessions": sessions}
            )

            result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

        self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_omx_tracking_thread_key_must_match_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(
                project,
                "leader-thread",
                {
                    "child-thread": {
                        "thread_id": "different-thread",
                        "kind": "subagent",
                    }
                },
            )

            result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

        self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_omx_tracking_subagent_resolves_parent_and_indents_under_leader(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            leader = _codex_rollout(root, project, "leader", "leader-thread")
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(project, "leader-thread", {
                "leader-thread": {"thread_id": "leader-thread", "kind": "leader"},
                "child-thread": {"thread_id": "child-thread", "kind": "subagent"},
            })

            metadata = _load_codex_metadata((leader, child))
            now = datetime(2026, 9, 9, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now, wd_models.Config(), [leader, child],
                [(leader, now), (child, now)], 0, 10, metadata,
            )

        self.assertEqual(metadata[wd_metadata.target_key(child)].parent_session_id,
                         "leader-thread")
        self.assertEqual(
            list(wd_dashboard.dashboard_tree_prefixes(snapshot.rows).values()),
            ["", "└─ "],
        )

    def test_embedded_parent_precedes_omx_tracking_and_preserves_three_level_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            root_item = _codex_rollout(
                root, project, "root", "root-thread", source="cli"
            )
            leader = _codex_rollout(root, project, "leader", "leader-thread", source={
                "subagent": {"thread_spawn": {"parent_thread_id": "root-thread"}},
            })
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(project, "leader-thread", {
                "root-thread": {"thread_id": "root-thread", "kind": "subagent"},
                "leader-thread": {"thread_id": "leader-thread", "kind": "leader"},
                "child-thread": {"thread_id": "child-thread", "kind": "subagent"},
            })

            items = (root_item, leader, child)
            metadata = _load_codex_metadata(items)
            now = datetime(2026, 9, 9, tzinfo=timezone.utc)
            snapshot = wd_dashboard.make_dashboard_snapshot(
                now, wd_models.Config(), list(items),
                [(item, now) for item in items], 0, 10, metadata,
            )

        self.assertEqual(
            [metadata[wd_metadata.target_key(item)].parent_session_id for item in items],
            [wd_models.UNKNOWN, "root-thread", "leader-thread"],
        )
        self.assertEqual(
            list(wd_dashboard.dashboard_tree_prefixes(snapshot.rows).values()),
            ["", "└─ ", "   └─ "],
        )

    def test_embedded_parent_beats_different_omx_tracking_leader_for_tracked_subagent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            embedded_parent = _codex_rollout(
                root, project, "embedded-parent", "embedded-parent-thread"
            )
            tracking_leader = _codex_rollout(
                root, project, "tracking-leader", "tracking-leader-thread"
            )
            child = _codex_rollout(
                root,
                project,
                "child",
                "child-thread",
                source={
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": "embedded-parent-thread"
                        }
                    }
                },
            )
            _write_omx_tracking(
                project,
                "tracking-leader-thread",
                {
                    "child-thread": {
                        "thread_id": "child-thread",
                        "kind": "subagent",
                    }
                },
            )

            items = (embedded_parent, tracking_leader, child)
            metadata = _load_codex_metadata(items)
            child_key = wd_metadata.target_key(child)
            child_metadata = metadata[child_key]

            self.assertEqual(
                child_metadata.parent_session_id, "embedded-parent-thread"
            )
            self.assertEqual(
                child_metadata.tracking_parent_session_id, wd_models.UNKNOWN
            )

            metadata[child_key] = replace(
                child_metadata,
                tracking_parent_session_id="tracking-leader-thread",
            )
            reapplied = wd_metadata.apply_omx_tracking_lineage(metadata)

        self.assertEqual(
            reapplied[child_key].parent_session_id, "embedded-parent-thread"
        )

    def test_omx_tracking_unsupported_schema_yields_no_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            child = _codex_rollout(root, project, "child", "child-thread")
            _write_omx_tracking(
                project, "leader-thread",
                {"child-thread": {"thread_id": "child-thread", "kind": "subagent"}},
                schema_version=2,
            )

            result = _load_codex_metadata((child,))[wd_metadata.target_key(child)]

        self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_absent_malformed_and_truncated_omx_tracking_yield_no_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in (
                ("absent", None),
                ("malformed", "not-json"),
                ("truncated", '{"schemaVersion": 1, "sessions":'),
            ):
                with self.subTest(name=name):
                    project = root / name
                    project.mkdir()
                    child = _codex_rollout(root, project, name, f"{name}-thread")
                    if content is not None:
                        path = project / ".omx" / "state" / "subagent-tracking.json"
                        path.parent.mkdir(parents=True)
                        path.write_text(content, encoding="utf-8")

                    result = _load_codex_metadata((child,))[
                        wd_metadata.target_key(child)
                    ]

                    self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_omx_tracking_leader_does_not_parent_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "synthetic-project"
            project.mkdir()
            leader = _codex_rollout(root, project, "leader", "leader-thread")
            _write_omx_tracking(project, "leader-thread", {
                "leader-thread": {"thread_id": "leader-thread", "kind": "leader"},
            })

            result = _load_codex_metadata((leader,))[
                wd_metadata.target_key(leader)
            ]

        self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

    def test_omx_tracking_rejects_unbounded_session_and_thread_collections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, document in (
                ("sessions", {
                    "schemaVersion": 1,
                    "sessions": {
                        f"omx-launch-{index}": {
                            "session_id": f"omx-launch-{index}",
                            "leader_thread_id": "leader-thread",
                            "threads": ({
                                "child-thread": {
                                    "thread_id": "child-thread",
                                    "kind": "subagent",
                                },
                            } if index == 0 else {}),
                        }
                        for index in range(wd_models.MAX_OMX_TRACKING_SESSIONS + 1)
                    },
                }),
                ("threads", {
                    "schemaVersion": 1,
                    "sessions": {
                        "omx-synthetic-launch": {
                            "session_id": "omx-synthetic-launch",
                            "leader_thread_id": "leader-thread",
                            "threads": {
                                **{
                                    f"unrelated-{index}": {
                                        "thread_id": f"unrelated-{index}",
                                        "kind": "subagent",
                                    }
                                    for index in range(wd_models.MAX_OMX_TRACKING_THREADS)
                                },
                                "child-thread": {
                                    "thread_id": "child-thread",
                                    "kind": "subagent",
                                },
                            },
                        },
                    },
                }),
            ):
                with self.subTest(name=name):
                    project = root / name
                    project.mkdir()
                    child = _codex_rollout(root, project, name, "child-thread")
                    path = project / ".omx" / "state" / "subagent-tracking.json"
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps(document), encoding="utf-8")

                    result = _load_codex_metadata((child,))[
                        wd_metadata.target_key(child)
                    ]

                    self.assertEqual(result.parent_session_id, wd_models.UNKNOWN)

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
