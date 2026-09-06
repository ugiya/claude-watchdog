#!/usr/bin/env python3
"""Run a safe, synthetic claude-watchdog dashboard demonstration.

The demo imports the real presentation code, but supplies fixed in-memory
snapshots.  It never discovers local sessions or starts power-management
processes.
"""

from __future__ import annotations

import argparse
import html
import importlib.machinery
import importlib.util
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "claude-watchdog"
BASE_TIME = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
FRAME_OFFSETS = (8, 18, 32, 48, 62, 70)
FRAME_DELAYS = (0.0, 4.0, 4.0, 4.0, 4.0, 4.0)
WIDTH = 178
HEIGHT = 22
RESET = "\x1b[0m"
CLEAR = "\x1b[2J\x1b[H"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


def load_watchdog():
    """Load the extensionless runtime without invoking its CLI entry point."""
    loader = importlib.machinery.SourceFileLoader("watchdog_demo_runtime", str(TARGET))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


watchdog = load_watchdog()


def _row(
    source: str,
    client: str,
    task: str,
    model: str,
    effort: str,
    session_id: str,
    event_offset: int,
    now: datetime,
    *,
    parent: str = "unknown",
    agent: str = "orchestrator",
    namespace: str | None = None,
) -> object:
    event = BASE_TIME + timedelta(seconds=event_offset)
    return watchdog.SessionRow(
        key=(source, f"/demo/{source}/{session_id}.jsonl"),
        source=source,
        client=client,
        task=task,
        model=model,
        effort=effort,
        started=event - timedelta(minutes=4),
        last_event=event,
        quiet_remaining=max(0.0, 60 - (now - event).total_seconds()),
        path=f"/demo/{source}/{session_id}.jsonl",
        identity_count=1,
        provenance="synthetic-demo",
        holding=(now - event).total_seconds() < 60,
        agent=agent,
        session_id=session_id,
        parent_session_id=parent,
        lineage_namespace=namespace or f"demo-{source}",
    )


def build_snapshots() -> tuple[object, ...]:
    """Return deterministic snapshots that demonstrate discovery and quieting."""
    snapshots = []
    for index, offset in enumerate(FRAME_OFFSETS):
        now = BASE_TIME + timedelta(seconds=offset)
        rows = [
            _row("claude", "Claude Code", "Build the overnight report", "claude-example", "high", "claude-root", 0, now),
            _row("claude", "Claude Code", "Review terminal hierarchy", "claude-example", "medium", "claude-child", 2, now,
                 parent="claude-root", agent="reviewer"),
        ]
        notice = ""
        if index >= 1:
            rows.append(_row("codex", "Codex", "Prepare the public release", "gpt-example", "high", "codex-root", 3, now))
            if index == 1:
                notice = "admitted 1 new Codex target"
        if index >= 2:
            rows.append(_row("codex", "Codex", "Verify install and rollback", "gpt-example", "medium", "codex-child", 5, now,
                             parent="codex-root", agent="verifier"))
            open_children = (
                watchdog.SessionChildMetadata(
                    "open-root", task="Design a searchable activity view", model="opencode-example",
                    effort="high", agent="build", started=BASE_TIME - timedelta(minutes=2),
                ),
                watchdog.SessionChildMetadata(
                    "open-child", "open-root", "Inspect the query planner", "opencode-example",
                    "medium", "explore", BASE_TIME - timedelta(minutes=1),
                ),
            )
            open_event = BASE_TIME + timedelta(seconds=8)
            rows.append(watchdog.SessionRow(
                key=("opencode", "/demo/opencode/activity.db"),
                source="opencode", client="OpenCode",
                task="Design a searchable activity view | Inspect the query planner",
                model="opencode-example", effort="high",
                started=BASE_TIME - timedelta(minutes=2), last_event=open_event,
                quiet_remaining=max(0.0, 60 - (now - open_event).total_seconds()),
                path="/demo/opencode/activity.db", identity_count=1,
                provenance="synthetic-demo", holding=(now - open_event).total_seconds() < 60,
                agent="build", lineage_namespace="demo-opencode", children=open_children,
                details=("Two synthetic sessions share one OpenCode activity guard.",),
            ))
            if index == 2:
                notice = "admitted 2 new targets (Codex subagent + OpenCode group)"
        holding = sum(row.holding for row in rows)
        watched = len(rows)
        snapshots.append(watchdog.DashboardSnapshot(
            now=now,
            rows=tuple(rows),
            watched_count=watched,
            holding_count=holding,
            session_quiet=holding == 0,
            user_idle=300.0 if holding == 0 else None,
            user_idle_required=300.0,
            next_poll_seconds=max(0.0, 10 - index),
            source="auto",
            discovery="live",
            idle_seconds=60.0,
            admission_notice=notice,
        ))
    return tuple(snapshots)


TOKEN_COLORS = {
    "CLAUDE": "\x1b[38;5;221m",
    "CODEX": "\x1b[38;5;45m",
    "OPENCODE": "\x1b[38;5;114m",
    "Claude Code": "\x1b[38;5;213m",
    "Codex": "\x1b[38;5;81m",
    "OpenCode": "\x1b[38;5;156m",
    "claude-example": "\x1b[38;5;177m",
    "gpt-example": "\x1b[38;5;81m",
    "opencode-example": "\x1b[38;5;208m",
}


def colorize(line: str) -> str:
    """Apply stable ANSI colors to labels produced by the real renderer."""
    parts = [line]
    for token in sorted(TOKEN_COLORS, key=len, reverse=True):
        next_parts = []
        for part in parts:
            if part in TOKEN_COLORS:
                next_parts.append(part)
                continue
            pieces = part.split(token)
            for position, piece in enumerate(pieces):
                if piece:
                    next_parts.append(piece)
                if position < len(pieces) - 1:
                    next_parts.append(token)
        parts = next_parts
    return "".join(
        f"{TOKEN_COLORS[part]}{part}{RESET}" if part in TOKEN_COLORS else part
        for part in parts
    )


def dashboard_frame(snapshot: object, *, ansi: bool = True) -> str:
    state = watchdog.DashboardState(sort="recent", tree=True)
    lines = watchdog.dashboard_lines(snapshot, state, width=WIDTH, height=HEIGHT)
    if ansi:
        lines = [colorize(line) for line in lines]
    return "\n".join(lines).rstrip() + "\n"


def final_report(snapshots: tuple[object, ...], *, ansi: bool = True) -> str:
    with _utc_timezone():
        history = watchdog.WatchHistory()
        for snapshot in snapshots:
            history.observe(snapshot)
        config = watchdog.Config(idle_minutes=1, user_idle_minutes=5, dry_run=True)
        lines = watchdog.exit_report_lines(history, config, 0, "synthetic demo complete", color=False)
    local_log = str(watchdog.LOG_FILE)
    lines = [line.replace(local_log, "/demo/claude-watchdog.log") for line in lines]
    if ansi:
        lines = [colorize(line) for line in lines]
    return "\n".join(lines) + "\n"


@contextmanager
def _utc_timezone():
    """Make report clocks deterministic without changing the parent shell."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        if hasattr(time, "tzset"):
            time.tzset()


def run_demo(*, stream: TextIO = sys.stdout, speed: float = 1.0, no_delay: bool = False) -> None:
    snapshots = build_snapshots()
    stream.write(HIDE_CURSOR)
    stream.flush()
    try:
        for delay, snapshot in zip(FRAME_DELAYS, snapshots):
            if delay and not no_delay:
                time.sleep(delay / speed)
            stream.write(CLEAR + dashboard_frame(snapshot))
            stream.flush()
        if not no_delay:
            time.sleep(2.0 / speed)
        stream.write(CLEAR + final_report(snapshots))
        stream.flush()
    finally:
        stream.write(SHOW_CURSOR)
        stream.flush()


def _svg_segments(line: str):
    segments = [(line, "#d6d9df")]
    svg_colors = {
        "CLAUDE": "#ffd75f", "CODEX": "#00d7d7", "OPENCODE": "#87d75f",
        "Claude Code": "#ff87ff", "Codex": "#5fd7ff", "OpenCode": "#afff87",
        "claude-example": "#d787ff", "gpt-example": "#5fd7ff", "opencode-example": "#ff8700",
    }
    for token in sorted(svg_colors, key=len, reverse=True):
        next_segments = []
        for text, color in segments:
            if color != "#d6d9df":
                next_segments.append((text, color))
                continue
            pieces = text.split(token)
            for position, piece in enumerate(pieces):
                if piece:
                    next_segments.append((piece, color))
                if position < len(pieces) - 1:
                    next_segments.append((token, svg_colors[token]))
        segments = next_segments
    return segments


def svg_asset(snapshot: object) -> str:
    lines = watchdog.dashboard_lines(snapshot, watchdog.DashboardState(tree=True), width=100, height=HEIGHT)
    text = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="460" viewBox="0 0 1040 460">',
        '  <rect width="1040" height="460" rx="16" fill="#11151b"/>',
        '  <circle cx="24" cy="21" r="6" fill="#ff5f57"/><circle cx="43" cy="21" r="6" fill="#febc2e"/><circle cx="62" cy="21" r="6" fill="#28c840"/>',
        '  <text x="820" y="25" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12.5" fill="#858b98">synthetic data · safe demo</text>',
        '  <g font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="12.5">',
    ]
    for line_number, line in enumerate(lines):
        y = 48 + line_number * 18
        spans = []
        for segment, color in _svg_segments(line.rstrip()):
            spans.append(f'<tspan fill="{color}">{html.escape(segment)}</tspan>')
        text.append(f'    <text x="22" y="{y}" xml:space="preserve">{"".join(spans)}</text>')
    text.extend(("  </g>", "</svg>", ""))
    return "\n".join(text)


def cast_asset(snapshots: tuple[object, ...]) -> str:
    header = {"version": 2, "width": WIDTH, "height": HEIGHT, "timestamp": 1768514400,
              "env": {"SHELL": "/bin/sh", "TERM": "xterm-256color"},
              "title": "claude-watchdog synthetic demo"}
    events = []
    at = 0.0
    for delay, snapshot in zip(FRAME_DELAYS, snapshots):
        at += delay
        events.append([at, "o", CLEAR + dashboard_frame(snapshot)])
    at += 2.0
    events.append([at, "o", CLEAR + final_report(snapshots)])
    return "\n".join([json.dumps(header, sort_keys=True), *(json.dumps(event, ensure_ascii=False) for event in events)]) + "\n"


def write_assets(output_dir: Path = ROOT / "docs") -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshots = build_snapshots()
    svg_path = output_dir / "demo.svg"
    cast_path = output_dir / "demo.cast"
    svg_path.write_text(svg_asset(snapshots[-2]), encoding="utf-8")
    cast_path.write_text(cast_asset(snapshots), encoding="utf-8")
    return svg_path, cast_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speed", type=float, default=1.0, help="animation speed multiplier (default: 1)")
    parser.add_argument("--no-delay", action="store_true", help="render every frame immediately")
    parser.add_argument("--write-assets", action="store_true", help="regenerate docs/demo.svg and docs/demo.cast")
    args = parser.parse_args(argv)
    if args.speed <= 0:
        parser.error("--speed must be greater than zero")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.write_assets:
        for path in write_assets():
            print(path.relative_to(ROOT))
        return 0
    run_demo(speed=args.speed, no_delay=args.no_delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
