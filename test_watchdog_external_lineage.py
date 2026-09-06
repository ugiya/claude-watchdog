#!/usr/bin/env python3
"""Regression tests for explicit cross-provider lineage declarations."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


def _load_target():
    target = Path(__file__).with_name("claude-watchdog")
    loader = importlib.machinery.SourceFileLoader(
        "watchdog_external_lineage_target", str(target)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


watchdog = _load_target()
NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _item(path: str, source: str):
    return watchdog.ActivityFile(Path(path), source)


def _metadata(session_id: str, task: str, parent_session_id: str = "unknown"):
    return watchdog.SessionMetadata(
        client="client", task=task, model="model", effort="high",
        provenance="fixture", session_id=session_id,
        parent_session_id=parent_session_id,
    )


def _write_registry(path: Path, links: list[dict]):
    path.write_text(json.dumps({"version": 1, "links": links}), encoding="utf-8")


def _link(child_source, child_id, parent_source, parent_id, evidence="verified launch"):
    return {
        "child": {"source": child_source, "session_id": child_id},
        "parent": {"source": parent_source, "session_id": parent_id},
        "evidence": evidence,
    }


class ExternalLineageTests(unittest.TestCase):
    def test_exact_cross_provider_ids_form_tree_without_title_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            _write_registry(registry, [
                _link("codex", "codex-child", "claude", "claude-parent"),
            ])
            parent = _item("/claude-parent.jsonl", "claude")
            child = _item("/codex-child.jsonl", "codex")
            similar = _item("/similar.jsonl", "codex")
            metadata = {
                watchdog.target_key(parent): _metadata("claude-parent", "Launch reviewer"),
                watchdog.target_key(child): _metadata("codex-child", "Review patch"),
                watchdog.target_key(similar): _metadata("other", "Launch reviewer"),
            }

            linked = watchdog.apply_external_lineage_registry(metadata, registry)
            snapshot = watchdog.make_dashboard_snapshot(
                NOW, watchdog.Config(), [similar, child, parent],
                [(similar, NOW), (child, NOW), (parent, NOW)], 0, 10, linked,
            )

        visible = watchdog.visible_dashboard_rows(
            snapshot.rows, watchdog.DashboardState(sort="title")
        )
        self.assertEqual(
            [row.task for row in visible],
            ["Launch reviewer", "Review patch", "Launch reviewer"],
        )
        self.assertEqual(visible[1].external_parent_key, watchdog.target_key(parent))
        self.assertIsNone(visible[2].external_parent_key)
        self.assertEqual(
            list(watchdog.dashboard_tree_prefixes(visible).values()),
            ["", "└─ ", ""],
        )
        self.assertTrue(any("verified launch" in line for line in visible[1].details))

    def test_conflicting_missing_ambiguous_and_native_links_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            _write_registry(registry, [
                _link("codex", "conflict", "claude", "parent-a"),
                _link("codex", "conflict", "claude", "parent-b"),
                _link("codex", "missing-parent", "claude", "absent"),
                _link("codex", "ambiguous", "claude", "parent-a"),
                _link("codex", "native", "claude", "parent-a"),
                _link("opencode", "unsupported", "claude", "parent-a"),
            ])
            values = {
                ("claude", "/a"): _metadata("parent-a", "Parent A"),
                ("claude", "/b"): _metadata("parent-b", "Parent B"),
                ("codex", "/conflict"): _metadata("conflict", "Conflict"),
                ("codex", "/missing"): _metadata("missing-parent", "Missing"),
                ("codex", "/ambiguous-1"): _metadata("ambiguous", "Ambiguous 1"),
                ("codex", "/ambiguous-2"): _metadata("ambiguous", "Ambiguous 2"),
                ("codex", "/native"): _metadata(
                    "native", "Native", parent_session_id="native-parent"
                ),
                ("opencode", "/unsupported"): _metadata(
                    "unsupported", "Unsupported"
                ),
            }

            linked = watchdog.apply_external_lineage_registry(values, registry)

        for key in values:
            self.assertIsNone(linked[key].external_parent_key)
            self.assertEqual(linked[key].details, ())

    def test_missing_malformed_oversized_and_excess_link_files_are_noops(self):
        values = {("codex", "/child"): _metadata("child", "Child")}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = {
                "missing": root / "missing.json",
                "malformed": root / "malformed.json",
                "deeply-nested": root / "deeply-nested.json",
                "oversized": root / "oversized.json",
                "too-many": root / "too-many.json",
            }
            cases["malformed"].write_text("{", encoding="utf-8")
            cases["deeply-nested"].write_bytes(b"[" * 10_000 + b"]" * 10_000)
            cases["oversized"].write_bytes(
                b" " * (watchdog.MAX_EXTERNAL_LINEAGE_BYTES + 1)
            )
            _write_registry(
                cases["too-many"],
                [
                    _link("codex", f"child-{index}", "claude", "parent")
                    for index in range(watchdog.MAX_EXTERNAL_LINEAGE_LINKS + 1)
                ],
            )

            for name, path in cases.items():
                with self.subTest(name=name):
                    self.assertEqual(
                        watchdog.apply_external_lineage_registry(values, path), values
                    )

    def test_filtering_out_external_parent_leaves_child_as_safe_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            _write_registry(registry, [
                _link("codex", "child", "claude", "parent"),
            ])
            parent = _item("/parent", "claude")
            child = _item("/child", "codex")
            metadata = watchdog.apply_external_lineage_registry({
                watchdog.target_key(parent): _metadata("parent", "Orchestrator"),
                watchdog.target_key(child): _metadata("child", "Needle task"),
            }, registry)
            snapshot = watchdog.make_dashboard_snapshot(
                NOW, watchdog.Config(), [parent, child], [(parent, NOW), (child, NOW)],
                0, 10, metadata,
            )

        visible = watchdog.visible_dashboard_rows(
            snapshot.rows, watchdog.DashboardState(query="needle")
        )
        self.assertEqual([row.task for row in visible], ["Needle task"])
        self.assertEqual(watchdog.dashboard_tree_prefixes(visible), {
            visible[0].key: "",
        })

    def test_registry_is_applied_after_all_provider_metadata_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            _write_registry(registry, [
                _link("claude", "child", "claude", "parent"),
            ])
            parent = _item("/parent", "claude")
            child = watchdog.ActivityFile(
                Path("/work/child"), "claude", profile_id="work", profile_label="Work"
            )
            loaded = {
                watchdog.target_key(parent): _metadata("parent", "Parent"),
                watchdog.target_key(child): _metadata("child", "Child"),
            }

            with (
                mock.patch.object(
                    watchdog, "load_session_metadata",
                    side_effect=lambda item, task_label="prompt": loaded[watchdog.target_key(item)],
                ),
                mock.patch.object(
                    watchdog, "external_lineage_registry_path", return_value=registry
                ),
            ):
                result = watchdog.load_dashboard_metadata([parent, child])

        self.assertEqual(
            result[watchdog.target_key(child)].external_parent_key,
            watchdog.target_key(parent),
        )

    def test_profile_native_trees_keep_reused_ids_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = []
            for profile in ("work", "personal"):
                directory = Path(tmp) / profile / "projects" / "project"
                directory.mkdir(parents=True)
                parent = directory / "parent.jsonl"
                child = directory / "parent" / "subagents" / "agent-child.jsonl"
                child.parent.mkdir(parents=True)
                for path, session_id in ((parent, "parent"), (child, "child")):
                    path.write_text(json.dumps({
                        "type": "assistant", "sessionId": session_id,
                        "timestamp": NOW.isoformat(), "message": {"model": "example-model"},
                    }) + "\n", encoding="utf-8")
                targets.extend(watchdog.ActivityFile(
                    path, "claude", profile_id=profile, profile_label=profile.title()
                ) for path in (parent, child))
            metadata = {watchdog.target_key(item): watchdog.jsonl_metadata(item) for item in targets}
            snapshot = watchdog.make_dashboard_snapshot(
                NOW, watchdog.Config(), targets, [(item, NOW) for item in targets], 0, 10, metadata
            )
            parents = watchdog._dashboard_parent_keys(snapshot.rows)
            self.assertEqual(parents, {
                watchdog.target_key(targets[1]): watchdog.target_key(targets[0]),
                watchdog.target_key(targets[3]): watchdog.target_key(targets[2]),
            })

    def test_external_parent_is_not_guessed_between_profiles_with_reused_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            _write_registry(registry, [_link("codex", "reviewer", "claude", "shared-parent")])
            parents = [watchdog.ActivityFile(
                Path(tmp) / profile / "parent.jsonl", "claude",
                profile_id=profile, profile_label=profile.title(),
            ) for profile in ("work", "personal")]
            child = _item(str(Path(tmp) / "reviewer.jsonl"), "codex")
            metadata = {watchdog.target_key(item): _metadata("shared-parent", "Parent") for item in parents}
            metadata[watchdog.target_key(child)] = _metadata("reviewer", "Review")
            with (
                mock.patch.object(watchdog, "external_lineage_registry_path", return_value=registry),
                mock.patch.object(watchdog, "load_session_metadata", side_effect=lambda item, **kw: metadata[watchdog.target_key(item)]),
            ):
                result = watchdog.load_dashboard_metadata([*parents, child])
            self.assertIsNone(result[watchdog.target_key(child)].external_parent_key)

    def test_evidence_is_sanitized_and_bounded_only_for_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            _write_registry(registry, [
                _link(
                    "codex", "child", "claude", "parent",
                    "launch\nproof\x1b[2J" + "x" * 1000,
                ),
            ])
            child_key = ("codex", "/child")
            parent_key = ("claude", "/parent")
            linked = watchdog.apply_external_lineage_registry({
                child_key: _metadata("child", "Child"),
                parent_key: _metadata("parent", "Parent"),
            }, registry)

        detail = linked[child_key].details[-1]
        self.assertEqual(linked[child_key].external_parent_key, parent_key)
        self.assertNotIn("\n", detail)
        self.assertNotIn("\x1b", detail)
        self.assertLessEqual(watchdog.text_cells(detail), 512)

    def test_json_decoder_recursion_failure_is_a_noop(self):
        values = {("codex", "/child"): _metadata("child", "Child")}
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "lineage.json"
            registry.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                watchdog.json, "loads", side_effect=RecursionError("too deep")
            ):
                result = watchdog.apply_external_lineage_registry(values, registry)
        self.assertEqual(result, values)


if __name__ == "__main__":
    unittest.main()
