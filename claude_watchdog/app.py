"""Watch lifecycle and command composition root."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import activity as activity_module
from . import config as config_module
from . import dashboard as dashboard_module
from . import metadata as metadata_module
from . import models as models_module
from . import power as power_module
from . import reporting as reporting_module

def _source_guard_status(
    now: datetime,
    idle_seconds: float,
    watch_set: list[models_module.ActivityFile],
    activity: list[tuple[models_module.ActivityFile, datetime]],
) -> str:
    """Summarize how each provider affects the content-recency sleep guard."""
    source_order = list(dict.fromkeys(item.source for item in watch_set))
    freshest_by_source: dict[str, datetime] = {}
    for item, timestamp in activity:
        previous = freshest_by_source.get(item.source)
        if previous is None or timestamp > previous:
            freshest_by_source[item.source] = timestamp

    parts: list[str] = []
    for source in source_order:
        count = sum(item.source == source for item in watch_set)
        timestamp = freshest_by_source.get(source)
        age = activity_module._activity_age(now, timestamp) if timestamp is not None else None
        state = "holding" if age is not None and age < idle_seconds else "quiet"
        event_age = "unavailable" if age is None else f"{int(age)}s ago"
        if source == "opencode":
            signal = "database activity"
            noun = "database" if count == 1 else "databases"
        else:
            signal = "JSONL event"
            noun = "file" if count == 1 else "files"
        parts.append(
            f"{source}={state} (last {signal} {event_age}; {count} {noun})"
        )
    return ", ".join(parts)


def _log_admissions(
    previous: list[models_module.ActivityFile], current: list[models_module.ActivityFile]
) -> None:
    previous_by_key = {(item.source, item.path): item for item in previous}
    for item in current:
        prior = previous_by_key.get((item.source, item.path))
        if prior is None:
            if item.source == "omx":
                models_module.log.info(
                    "admitted new activity target: %s (%d OMX identity/identities)",
                    item.label,
                    len(item.identities),
                )
            elif item.source == "opencode":
                models_module.log.info(
                    "admitted new activity target: %s (%d OpenCode lineage seed(s))",
                    item.label,
                    len(item.identities),
                )
            else:
                models_module.log.info("admitted new activity target: %s", item.label)
            continue
        added = item.identities - prior.identities
        if added and item.source == "omx":
            models_module.log.info(
                "admitted %d new OMX identity/identities for %s",
                len(added),
                item.label,
            )
        elif added and item.source == "opencode":
            models_module.log.info(
                "admitted %d new OpenCode lineage seed(s) for %s",
                len(added),
                item.label,
            )


def dashboard_admission_notice(
    previous: list[models_module.ActivityFile], current: list[models_module.ActivityFile]
) -> str:
    """Describe additive target/identity changes without changing stable row keys."""
    previous_by_key = {metadata_module.target_key(item): item for item in previous}
    new_targets = 0
    identity_counts: dict[str, int] = {}
    for item in current:
        prior = previous_by_key.get(metadata_module.target_key(item))
        if prior is None:
            new_targets += 1
            continue
        added = item.identities - prior.identities
        if added:
            identity_counts[item.source] = identity_counts.get(item.source, 0) + len(added)
    parts = []
    if new_targets:
        noun = "target" if new_targets == 1 else "targets"
        parts.append(f"admitted {new_targets} new {noun}")
    for source, count in sorted(identity_counts.items()):
        if source == "opencode":
            noun = "lineage seed" if count == 1 else "lineage seeds"
            parts.append(f"admitted {count} new OpenCode {noun}")
        else:
            noun = "identity" if count == 1 else "identities"
            parts.append(f"admitted {count} new {source.upper()} {noun}")
    return " · ".join(parts)


def wait_until_quiet(
    cfg: models_module.Config,
    watch_set: list[models_module.ActivityFile],
    dashboard: dashboard_module.TerminalDashboard | None = None,
) -> None:
    """Wait for every watched session to be quiet and for the user to be idle."""
    while True:
        now = datetime.now(timezone.utc)
        admission_notice = ""
        if cfg.session_discovery == "live":
            previous = watch_set
            watch_set = activity_module.refresh_watch_set(cfg, previous, now=now)
            _log_admissions(previous, watch_set)
            admission_notice = dashboard_admission_notice(previous, watch_set)
        activity: list[tuple[models_module.ActivityFile, datetime]] = []
        for item in watch_set:
            timestamp = activity_module._last_activity_for(item, now)
            if timestamp is not None:
                activity.append((item, timestamp))

        _, freshest = (
            max(activity, key=lambda pair: pair[1]) if activity else (None, None)
        )
        age = activity_module._activity_age(now, freshest) if freshest else None
        sessions_quiet = age is None or age >= cfg.idle_seconds
        idle = (
            power_module.user_idle_seconds()
            if sessions_quiet and cfg.user_idle_seconds > 0
            else (0.0 if cfg.user_idle_seconds == 0 else None)
        )
        user_quiet = cfg.user_idle_seconds == 0 or (
            idle is not None and idle >= cfg.user_idle_seconds
        )
        source_status = _source_guard_status(
            now,
            cfg.idle_seconds,
            watch_set,
            activity,
        )

        if dashboard is not None:
            metadata = metadata_module.load_dashboard_metadata(watch_set, cfg.task_label)
            snapshot = dashboard_module.make_dashboard_snapshot(
                now, cfg, watch_set, activity, idle, cfg.poll_seconds, metadata,
                admission_notice,
            )
            dashboard.history.observe(snapshot)
            dashboard.update(snapshot)

        if sessions_quiet and user_quiet:
            models_module.log.info(
                "all source guards quiet (%s), user idle %ds -> %s",
                source_status,
                int(idle or 0),
                "dry-run sleep decision" if cfg.dry_run else "sleeping Mac",
            )
            return
        if sessions_quiet:
            models_module.log.info(
                "all source guards quiet (%s) but user active (idle %ds); holding",
                source_status,
                int(idle or 0),
            )
        else:
            models_module.log.info("source guards: %s; still awake", source_status)
        if dashboard is None:
            time.sleep(cfg.poll_seconds)
        else:
            dashboard.wait(cfg.poll_seconds)


def setup_logging() -> None:
    if models_module.log.handlers:
        return
    models_module.log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [watchdog] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(models_module.LOG_FILE)):
        handler.setFormatter(fmt)
        models_module.log.addHandler(handler)


def _run_watch_phase(
    cfg: models_module.Config,
    watch_set: list[models_module.ActivityFile],
    dashboard: dashboard_module.TerminalDashboard | None,
) -> tuple[subprocess.Popen | None, int, tuple[int, str, tuple[object, ...]]]:
    """Start the wake assertion and run the loop without performing cleanup or logging."""
    try:
        process = power_module.block_sleep()
    except KeyboardInterrupt:
        return None, 130, (
            logging.INFO,
            "interrupted while starting caffeinate; exiting without sleeping",
            (),
        )
    except OSError as exc:
        return None, 1, (
            logging.ERROR,
            "unable to start caffeinate; exiting without sleeping: %s",
            (exc,),
        )

    try:
        if dashboard is None:
            wait_until_quiet(cfg, watch_set)
        else:
            wait_until_quiet(cfg, watch_set, dashboard)
    except KeyboardInterrupt:
        return process, 130, (
            logging.INFO,
            "interrupted; wake assertion released without sleeping",
            (),
        )
    except models_module.WatchdogError as exc:
        return process, 1, (
            logging.ERROR,
            "watch aborted because activity state is uncertain; wake assertion "
            "released and sleep skipped: %s",
            (exc,),
        )
    except Exception as exc:
        return process, 1, (
            logging.ERROR,
            "watch aborted after an unexpected display/runtime error; wake assertion "
            "released and sleep skipped: %s",
            (exc,),
        )
    return process, 0, (
        logging.INFO,
        "all guards satisfied; wake assertion released%s",
        ("; dry-run sleep decision follows" if cfg.dry_run else "; sleeping Mac",),
    )


def _release_watch_process(
    process: subprocess.Popen | None,
    status: int,
    record: tuple[int, str, tuple[object, ...]],
) -> tuple[int, tuple[int, str, tuple[object, ...]]]:
    """Release an owned wake assertion after terminal state has been restored."""
    if process is None:
        return status, record
    try:
        power_module._stop_caffeinate(process)
    except KeyboardInterrupt:
        try:
            power_module._stop_caffeinate(process)
        except BaseException:
            pass
        return 130, (
            logging.INFO,
            "interrupted while releasing caffeinate; exiting without sleeping",
            (),
        )
    except Exception as exc:
        return 1, (
            logging.ERROR,
            "unable to release caffeinate cleanly; exiting without sleeping: %s",
            (exc,),
        )
    return status, record


def _emit_outcome(record: tuple[int, str, tuple[object, ...]]) -> None:
    level, message, arguments = record
    models_module.log.log(level, message, *arguments)


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = config_module.parse_args(argv)
    except models_module.WatchdogError as exc:
        print(f"claude-watchdog: error: {exc}", file=sys.stderr)
        return 1
    setup_logging()
    models_module.log.info(
        "start: source=%s session-discovery=%s task-label=%s idle>=%gm, "
        "user-idle gate %gm, poll %gs%s",
        cfg.source,
        cfg.session_discovery,
        cfg.task_label,
        cfg.idle_minutes,
        cfg.user_idle_minutes,
        cfg.poll_seconds,
        " [dry-run]" if cfg.dry_run else "",
    )

    try:
        candidates = (
            activity_module.activity_files(cfg.source, cfg.claude_profiles)
            if cfg.claude_profiles else activity_module.activity_files(cfg.source)
        )
        launch_time = datetime.now(timezone.utc)
        watch_set = activity_module.select_watch_set(cfg, candidates, now=launch_time)
    except KeyboardInterrupt:
        models_module.log.info("interrupted before watch started; nothing will be put to sleep")
        return 130
    except models_module.WatchdogError as exc:
        models_module.log.error("launch selection failed; exiting without sleeping: %s", exc)
        return 1

    if not watch_set:
        models_module.log.error(
            "no active %s session in the launch snapshot; exiting without sleeping",
            cfg.source,
        )
        return 1

    models_module.log.info("watching %d %s activity target(s):", len(watch_set), cfg.source)
    for item in watch_set:
        models_module.log.info("    - %s", item.label)
    models_module.log.info(
        "status is based on persisted activity timestamps, not process liveness; "
        "holding means the source still prevents sleep"
    )

    display_mode = dashboard_module.resolve_display(cfg.display)
    finished_dashboard = None
    process = None
    status = 1
    record: tuple[int, str, tuple[object, ...]] = (
        logging.ERROR,
        "watch did not start; exiting without sleeping",
        (),
    )
    if display_mode == "dashboard":
        try:
            with dashboard_module.dashboard_context(cfg) as dashboard:
                finished_dashboard = dashboard
                process, status, record = _run_watch_phase(cfg, watch_set, dashboard)
        except KeyboardInterrupt:
            status, record = 130, (
                logging.INFO,
                "interrupted while opening or restoring dashboard; exiting without sleeping",
                (),
            )
        except models_module.WatchdogError as exc:
            if cfg.display == "auto" and process is None and not isinstance(exc, models_module.TerminalRestoreError):
                models_module.log.warning("%s; continuing with log display", exc)
                process, status, record = _run_watch_phase(cfg, watch_set, None)
            elif process is not None:
                status, record = 1, (
                    logging.ERROR,
                    "dashboard teardown failed; wake assertion released and sleep skipped: %s",
                    (exc,),
                )
            else:
                models_module.log.error("%s; exiting without sleeping", exc)
                return 1
    else:
        if cfg.display == "auto":
            models_module.log.info("interactive dashboard unavailable on this terminal; using log display")
        process, status, record = _run_watch_phase(cfg, watch_set, None)

    status, record = _release_watch_process(process, status, record)
    if finished_dashboard is not None:
        try:
            reporting_module.emit_exit_report(finished_dashboard.history, cfg, status, record[1] % record[2])
        except KeyboardInterrupt:
            models_module.log.info("interrupted while writing exit report; sleep skipped")
            return 130
        except Exception as exc:
            models_module.log.error("unable to write exit report; sleep skipped: %s", exc)
            return 1
    _emit_outcome(record)
    if status != 0:
        return status

    try:
        power_module.force_sleep(cfg.dry_run)
    except models_module.WatchdogError as exc:
        models_module.log.error("sleep command failed: %s", exc)
        return 1
    return 0
