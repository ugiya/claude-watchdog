#!/usr/bin/env python3
"""Smoke tests for the synthetic public demo and checked-in assets."""

from __future__ import annotations

import importlib.util
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("watchdog_demo", ROOT / "scripts" / "demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class SyntheticDemoTests(unittest.TestCase):
    def render_assets_in_timezone(self, zone):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = zone
        if hasattr(time, "tzset"):
            time.tzset()
        try:
            snapshots = demo.build_snapshots()
            return demo.svg_asset(snapshots[-2]), demo.cast_asset(snapshots)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            if hasattr(time, "tzset"):
                time.tzset()

    def test_story_covers_trees_discovery_countdown_and_final_report(self):
        snapshots = demo.build_snapshots()
        first, discovered, final = snapshots[0], snapshots[2], snapshots[-1]

        self.assertEqual((first.watched_count, len(first.rows)), (2, 2))
        self.assertIn("admitted 2 new targets", discovered.admission_notice)
        visible = demo.watchdog.visible_dashboard_rows(
            discovered.rows, demo.watchdog.DashboardState(tree=True)
        )
        prefixes = demo.watchdog.dashboard_tree_prefixes(visible)
        rendered = demo.dashboard_frame(discovered, ansi=False)
        self.assertIn("└─ Review terminal hierarchy", rendered)
        self.assertIn("└─ Verify install and rollback", rendered)
        self.assertIn("OpenCode", rendered)
        self.assertIn("Inspect the query planner", rendered)
        self.assertTrue(any(prefix for prefix in prefixes.values()))
        self.assertEqual((final.holding_count, final.session_quiet), (0, True))
        report = demo.final_report(snapshots, ansi=False)
        self.assertIn("DRY RUN — no sleep request", report)
        self.assertIn("All session guards quiet", report)
        self.assertIn("FINAL SESSIONS", report)

    def test_no_delay_demo_is_ansi_and_never_enters_runtime_or_power_paths(self):
        output = io.StringIO()
        with mock.patch.object(demo.watchdog, "main", side_effect=AssertionError("runtime main")), \
             mock.patch.object(demo.watchdog, "block_sleep", side_effect=AssertionError("caffeinate")), \
             mock.patch.object(demo.watchdog, "force_sleep", side_effect=AssertionError("pmset")):
            demo.run_demo(stream=output, no_delay=True)
        rendered = output.getvalue()
        self.assertIn(demo.HIDE_CURSOR, rendered)
        self.assertIn(demo.CLEAR, rendered)
        self.assertIn(demo.SHOW_CURSOR, rendered)
        self.assertIn("claude-example", rendered)
        self.assertIn("gpt-example", rendered)

    def test_assets_are_deterministic_match_sources_and_contain_no_private_paths(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_paths = demo.write_assets(Path(first))
            second_paths = demo.write_assets(Path(second))
            for left, right in zip(first_paths, second_paths):
                self.assertEqual(left.read_bytes(), right.read_bytes())
                self.assertEqual(left.read_bytes(), (ROOT / "docs" / left.name).read_bytes())

        assets = "".join((ROOT / "docs" / name).read_text(encoding="utf-8")
                         for name in ("demo.svg", "demo.cast"))
        for private_marker in ("/Users/", "/home/", "uri/projects", ".claude/sessions", ".codex/sessions"):
            self.assertNotIn(private_marker, assets)
        self.assertIn("claude-example", assets)
        self.assertIn("gpt-example", assets)
        self.assertIn("└─", assets)

    def test_assets_do_not_depend_on_process_timezone(self):
        utc = self.render_assets_in_timezone("UTC")
        jerusalem = self.render_assets_in_timezone("Asia/Jerusalem")
        los_angeles = self.render_assets_in_timezone("America/Los_Angeles")

        self.assertEqual(utc, jerusalem)
        self.assertEqual(utc, los_angeles)
        self.assertIn("2026-01-15 22:00:02 UTC", utc[1])
        self.assertNotIn(" IST", utc[1])
        self.assertNotIn(" PST", utc[1])


if __name__ == "__main__":
    unittest.main()
