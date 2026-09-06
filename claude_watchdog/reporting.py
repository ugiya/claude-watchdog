"""Bounded watch history and final run reports."""

from __future__ import annotations

import os
import sys
import textwrap
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import models as models_module
from . import presentation as presentation_module
from . import text as text_module

class WatchHistory:
    """Bounded observations from real polls, independent of UI repaint and filters."""
    def __init__(self):
        self.started: datetime | None = None
        self.snapshot: models_module.DashboardSnapshot | None = None
        self.events: deque[tuple[datetime, str]] = deque(maxlen=256)

    @staticmethod
    def user_ready(snapshot: models_module.DashboardSnapshot) -> bool:
        return snapshot.user_idle_required == 0 or (
            snapshot.user_idle is not None and snapshot.user_idle >= snapshot.user_idle_required
        )

    def observe(self, snapshot: models_module.DashboardSnapshot) -> None:
        previous = self.snapshot
        self.started = self.started or snapshot.now
        self.snapshot = snapshot
        messages = []
        if previous is None:
            messages.append(f"First observation: {snapshot.holding_count} holding / {snapshot.watched_count} targets")
        elif snapshot.session_quiet and not previous.session_quiet:
            messages.append("All session guards quiet; quiet countdown complete")
        elif previous.session_quiet and not snapshot.session_quiet:
            messages.append("Recorded activity resumed; quiet countdown reset")
        elif snapshot.holding_count != previous.holding_count:
            messages.append(f"{snapshot.holding_count} holding / {snapshot.watched_count} targets")
        if snapshot.admission_notice:
            messages.append(text_module.sanitize_terminal_text(snapshot.admission_notice))
        if snapshot.session_quiet:
            if (previous is not None and previous.session_quiet and
                    previous.user_idle is not None and snapshot.user_idle is not None and
                    snapshot.user_idle < previous.user_idle):
                messages.append("User-idle timer reset by input")
            if previous is None or not previous.session_quiet or self.user_ready(snapshot) != self.user_ready(previous):
                if self.user_ready(snapshot):
                    messages.append("User-idle gate satisfied; all guards ready")
                else:
                    remaining = max(0, snapshot.user_idle_required - (snapshot.user_idle or 0))
                    messages.append(f"Waiting for user-idle gate: {text_module._duration(remaining)} remaining if no input")
        checkpoint = not self.events or (snapshot.now - self.events[-1][0]).total_seconds() >= 900
        if messages or checkpoint:
            newest = max((row.last_event for row in snapshot.rows if row.last_event is not None), default=None)
            if not messages:
                messages.append(f"Checkpoint: {snapshot.holding_count} holding / {snapshot.watched_count} targets")
            if snapshot.session_quiet and snapshot.user_idle_required:
                remaining = max(0, snapshot.user_idle_required - (snapshot.user_idle or 0))
                messages.append(f"user {text_module._report_duration(snapshot.user_idle or 0)} idle / "
                                f"{text_module._report_duration(snapshot.user_idle_required)} required; "
                                f"{text_module._report_duration(remaining)} remaining if no input")
            if not snapshot.session_quiet and newest is not None:
                remaining = max(0, snapshot.idle_seconds - (snapshot.now - newest).total_seconds())
                messages.append(f"quiet in {text_module._duration(remaining)} if no new activity; last event {text_module._report_clock(newest)}")
            self.events.append((snapshot.now, "; ".join(messages)))
        cutoff = snapshot.now - timedelta(hours=6)
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()


def exit_report_lines(
    history: WatchHistory, cfg: models_module.Config, status: int, reason: str, *, color: bool = False,
) -> list[str]:
    """Normal-screen summary: printable text plus optional SGR, never cursor controls."""
    def paint(value: str, code: int | None = None, bold: bool = False) -> str:
        text = text_module.clip_cells(value, 512)
        if not color:
            return text
        style = "1" if bold else (f"38;5;{code}" if code is not None and code >= 8 else str(30 + code) if code is not None else "0")
        return f"\x1b[{style}m{text}\x1b[0m"

    outcome = "STOPPED — sleep skipped" if status == 130 else "ERROR — sleep skipped" if status else (
        "DRY RUN — no sleep request" if cfg.dry_run else "SLEEP READY — sleep request follows"
    )
    lines = ["", paint("CLAUDE WATCHDOG — RUN REPORT", bold=True), paint(outcome, bold=True)]
    if status and reason:
        lines.append(paint(reason))
    snapshot = history.snapshot
    if snapshot is None:
        lines.append("No successful session observation was captured.")
        lines.append(f"Detailed poll log: {paint(str(models_module.LOG_FILE))}")
        return lines
    lines.extend([
        f"Observed run: {text_module._report_clock(history.started)} → {text_module._report_clock(snapshot.now)}",
        f"Final poll: {snapshot.holding_count} holding / {snapshot.watched_count} targets; quiet threshold {cfg.idle_minutes:g}m",
        "Recorded activity indicates recency, not task completion or process liveness.",
        "",
        paint("COUNTDOWN AND SLEEP GATES", bold=True),
    ])
    newest = max((row.last_event for row in snapshot.rows if row.last_event is not None), default=None)
    if newest is None:
        lines.append("No usable recorded activity timestamp at the final poll.")
    else:
        lines.extend([
            f"Last recorded activity / countdown anchor: {text_module._report_clock(newest)}",
            f"Quiet deadline (if no newer activity): {text_module._report_clock(text_module._quiet_deadline(newest, snapshot.idle_seconds))}",
        ])
    lines.append(f"Session guards at final poll: {'QUIET' if snapshot.session_quiet else 'HOLDING'}")
    user_gate = "disabled" if snapshot.user_idle_required == 0 else (
        "not checked yet (sessions still holding)" if snapshot.user_idle is None else
        f"{text_module._report_duration(snapshot.user_idle)} idle / {text_module._report_duration(snapshot.user_idle_required)} required — "
        f"{'satisfied' if history.user_ready(snapshot) else 'waiting'}"
    )
    lines.extend([f"User-idle gate: {user_gate}", "", paint("RECENT TIMELINE — last 6 hours, up to 256 milestones", bold=True)])
    for at, message in history.events:
        lines.append(f"  {text_module._report_clock(at)}")
        lines.extend(textwrap.wrap(text_module.sanitize_terminal_text(message), width=100, initial_indent="    ", subsequent_indent="    "))
    lines.extend(["", paint("FINAL SESSIONS — last successful poll, all targets", bold=True)])
    palette = presentation_module.LABEL_COLORS if "256color" in os.environ.get("TERM", "") else (6, 5, 2, 3, 4, 1)
    clients: dict[str, int] = {}
    models: dict[str, int] = {}
    for row in snapshot.rows:
        for value, mapping in ((row.client, clients), (row.model, models)):
            if value != models_module.UNKNOWN and value not in mapping:
                mapping[value] = palette[len(mapping) % len(palette)]
    rows = sorted(snapshot.rows, key=lambda row: row.last_event or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for index, row in enumerate(rows, 1):
        source = paint(row.source.upper(), presentation_module.SOURCE_COLORS.get(row.source))
        client = paint(row.client, clients.get(row.client))
        model = row.model if row.effort == models_module.UNKNOWN else f"{row.model} / {row.effort}"
        label = row.task if row.task != models_module.UNKNOWN else Path(row.path).name
        lines.extend([
            f"  {index:02d}  {source} | {client} | {paint(label)}",
            f"      {paint(model, models.get(row.model))}",
            f"      Last event: {text_module._report_clock(row.last_event)} | Quiet at: {text_module._report_clock(text_module._quiet_deadline(row.last_event, snapshot.idle_seconds))}",
            f"      State: {'holding' if row.holding else 'quiet' if row.last_event else 'unavailable'} | Started: {text_module._report_clock(row.started)}",
            f"      agent {paint(row.agent)} | {row.identity_count} identities | metadata {paint(row.provenance)}",
            f"      Path: {paint(row.path)}",
        ])
        lines.extend(f"      {paint(detail)}" for detail in row.details)
    lines.extend(["", f"Detailed poll log: {text_module.sanitize_terminal_text(str(models_module.LOG_FILE))}", ""])
    return lines


def emit_exit_report(history: WatchHistory, cfg: models_module.Config, status: int, reason: str) -> None:
    color = presentation_module.colors_enabled(cfg) and sys.stdout.isatty() and os.environ.get("TERM", "dumb").lower() != "dumb"
    sys.stdout.write("\n".join(exit_report_lines(history, cfg, status, reason, color=color)) + "\n")
    sys.stdout.flush()
