"""Observational provider metadata and display lineage."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import activity as activity_module
from . import config as config_module
from . import models as models_module
from . import text as text_module

def target_key(item: models_module.ActivityFile) -> tuple[str, str]:
    """Stable display identity; it does not participate in activity decisions."""
    return item.source, str(item.path)


def _metadata_time(value: object) -> datetime | None:
    if isinstance(value, str):
        return activity_module._parse_ts(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _bounded_metadata_objects(
    path: Path, max_records: int, *, spread: bool = False
) -> list[dict[str, object]]:
    """Sample byte-bounded transcript windows, skipping cut records."""
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        if spread:
            window_size = min(models_module.READ_CHUNK_BYTES, size)
            window_count = min(
                models_module.MAX_METADATA_BYTES // models_module.READ_CHUNK_BYTES,
                max(1, math.ceil(size / models_module.READ_CHUNK_BYTES)),
            )
            span = size - window_size
            if window_count == 1:
                starts = (0,)
            elif window_count == 2:
                starts = (0, span)
            elif window_count == 3:
                starts = (0, round(span / 2), span)
            else:
                starts = (0, round(size / 4), round(size / 2), span)
            chunks = []
            for start in starts:
                handle.seek(start)
                chunks.append((handle.read(window_size), start > 0, start + window_size < size))
            # Claude transcripts can pack hundreds of small events into one window.
            # Scan every complete object inside the fixed byte budget so the actual
            # newest title, prompt, model, and effort cannot sit beyond a line quota.
            budgets = (None,) * len(chunks)
        else:
            head_size = min(models_module.READ_CHUNK_BYTES, size)
            handle.seek(0)
            head = handle.read(head_size)
            tail_start = max(head_size, size - (models_module.MAX_METADATA_BYTES - head_size))
            handle.seek(tail_start)
            tail = handle.read(models_module.MAX_METADATA_BYTES - head_size)
            chunks = [(head, False, head_size < size), (tail, tail_start > head_size, False)]
            tail_present = bool(tail)
            head_budget = max_records if not tail_present else max(1, max_records // 2)
            budgets = (head_budget, max(0, max_records - head_budget))
    objects: list[dict[str, object]] = []
    for (data, cut_left, cut_right), budget in zip(chunks, budgets):
        accepted = 0
        lines = data.splitlines()
        if cut_left and lines:
            lines = lines[1:]
        if cut_right and data and not data.endswith(b"\n") and lines:
            lines = lines[:-1]
        for raw in lines:
            if len(raw) > models_module.READ_CHUNK_BYTES:
                continue
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                objects.append(value)
                accepted += 1
                if budget is not None and accepted >= budget:
                    break
    return objects


def _has_omx_launch(session_id: object, cwd: object, started: object) -> bool:
    """Correlate explicit OMX launch metadata; project configuration is not evidence."""
    if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return False
    root = Path(cwd) / ".omx"

    def matches(value: object) -> bool:
        return (isinstance(value, dict) and value.get("native_session_id") == session_id
                and isinstance(value.get("session_id"), str) and value["session_id"].startswith("omx-"))

    try:
        with (root / "state/session.json").open("rb") as handle:
            if matches(json.loads(handle.read(models_module.READ_CHUNK_BYTES))):
                return True
    except (OSError, ValueError, RecursionError):
        pass
    timestamp = activity_module._parse_ts(started) if isinstance(started, str) else None
    if timestamp is None:
        return False
    # Launch and reconciliation can straddle midnight or use a local date.
    for offset in (0, -1, 1):
        try:
            day = timestamp + timedelta(days=offset)
            records = _bounded_metadata_objects(root / "logs" / day.strftime("omx-%Y-%m-%d.jsonl"), 200)
        except (OSError, OverflowError):
            continue
        if any(record.get("event") == "session_start_reconciled" and matches(record) for record in records):
            return True
    return False


def _omx_tracking_parent(session_id: object, cwd: object) -> str | None:
    """Return one exact, bounded OMX parent declaration for a Codex rollout."""
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
    ):
        return None
    try:
        path = Path(cwd) / ".omx/state/subagent-tracking.json"
        with path.open("rb") as handle:
            raw = handle.read(models_module.MAX_OMX_TRACKING_BYTES + 1)
        if len(raw) > models_module.MAX_OMX_TRACKING_BYTES:
            return None
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    schema_version = document.get("schemaVersion")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        return None
    sessions = document.get("sessions")
    if (
        not isinstance(sessions, dict)
        or len(sessions) > models_module.MAX_OMX_TRACKING_SESSIONS
    ):
        return None

    parents = set()
    for launch_id, value in sessions.items():
        if not isinstance(launch_id, str) or not isinstance(value, dict):
            continue
        tracked_session_id = value.get("session_id")
        leader_thread_id = value.get("leader_thread_id")
        threads = value.get("threads")
        if (
            not isinstance(tracked_session_id, str)
            or tracked_session_id != launch_id
            or not isinstance(leader_thread_id, str)
            or not leader_thread_id
            or not isinstance(threads, dict)
            or len(threads) > models_module.MAX_OMX_TRACKING_THREADS
        ):
            continue
        thread = threads.get(session_id)
        if not isinstance(thread, dict):
            continue
        thread_id = thread.get("thread_id")
        kind = thread.get("kind")
        if (
            not isinstance(thread_id, str)
            or thread_id != session_id
            or not isinstance(kind, str)
            or kind != "subagent"
            or leader_thread_id == session_id
        ):
            continue
        parents.add(leader_thread_id)
        if len(parents) > 1:
            return None
    return next(iter(parents), None)


def _claude_registry_name(session_id: str) -> str | None:
    """Return the name from an exact bounded Claude session-registry match."""
    try:
        entries = []
        with os.scandir(config_module.claude_sessions_dir()) as iterator:
            for index, entry in enumerate(iterator):
                if index >= models_module.MAX_CLAUDE_REGISTRY_FILES:
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    entries.append((info.st_mtime_ns, entry.path))
        budget = 2 * 1024 * 1024
        for _, raw_path in sorted(entries, reverse=True):
            if budget <= 0:
                break
            try:
                with Path(raw_path).open("rb") as handle:
                    data = handle.read(min(models_module.READ_CHUNK_BYTES, budget))
                budget -= len(data)
                value = json.loads(data)
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict) or value.get("sessionId") != session_id:
                continue
            name = value.get("name")
            return text_module._safe_metadata_value(name) if isinstance(name, str) and name.strip() else None
    except OSError:
        pass
    return None


def _fallback_task_label(item: models_module.ActivityFile, cwd: str, identifier: str) -> str:
    project = Path(cwd).name if cwd != models_module.UNKNOWN else item.path.parent.name
    return text_module.sanitize_terminal_text(f"{project} · {identifier[:8]}")


def jsonl_metadata(
    item: models_module.ActivityFile,
    max_records: int = 200,
    task_label: str = "prompt",
) -> models_module.SessionMetadata:
    """Read bounded metadata and, when selected, Claude's designated prompt."""
    client = task = model = effort = cwd = agent = models_module.UNKNOWN
    generated_task: str | None = None
    custom_task: str | None = None
    prompt_task: str | None = None
    started: datetime | None = None
    provenance = models_module.UNKNOWN
    claude_family = item.source == "claude"
    omx_launch = False
    session_id: str | None = None
    parent_session_id: str | None = None
    tracking_parent_session_id: str | None = None
    claude_agent_id: str | None = None
    try:
        records = _bounded_metadata_objects(item.path, max_records, spread=claude_family)
    except OSError:
        return models_module.SessionMetadata()

    for obj in records:
        timestamp = activity_module._parse_ts(obj.get("timestamp")) if isinstance(obj.get("timestamp"), str) else None
        if timestamp is not None and (started is None or timestamp < started):
            started = timestamp
        kind = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if kind == "session_meta":
            # Forked transcripts include inherited parent session_meta records.
            # The first valid identity belongs to this rollout, not its history.
            if item.source == "codex" and session_id is not None:
                continue
            client = text_module._safe_metadata_value(payload.get("originator") or payload.get("client") or client)
            agent = text_module._safe_metadata_value(payload.get("agent_nickname") or payload.get("agent_role") or agent)
            cwd = text_module._safe_metadata_value(payload.get("cwd") or cwd)
            model = text_module._safe_metadata_value(payload.get("model") or model)
            effort = text_module._safe_metadata_value(payload.get("reasoning_effort") or effort)
            provenance = "jsonl"
            if item.source == "codex":
                record_id = payload.get("id")
                if isinstance(record_id, str) and record_id:
                    session_id = record_id
                record_omx_launch = _has_omx_launch(
                    payload.get("session_id") or payload.get("id"),
                    payload.get("cwd"), payload.get("timestamp") or obj.get("timestamp"),
                )
                omx_launch = omx_launch or record_omx_launch
                source = payload.get("source")
                subagent = source.get("subagent") if isinstance(source, dict) else None
                thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                parent_id = thread_spawn.get("parent_thread_id") if isinstance(thread_spawn, dict) else None
                if isinstance(parent_id, str) and parent_id:
                    parent_session_id = parent_id
                elif not record_omx_launch:
                    tracking_parent_session_id = _omx_tracking_parent(
                        record_id, payload.get("cwd")
                    )
        elif kind == "turn_context":
            model = text_module._safe_metadata_value(payload.get("model") or model)
            effort = text_module._safe_metadata_value(payload.get("reasoning_effort") or effort)
            cwd = text_module._safe_metadata_value(payload.get("cwd") or cwd)
            provenance = "jsonl"
        elif kind in {"custom-title", "custom_title"}:
            title = payload.get("title") or obj.get("title")
            if isinstance(title, str) and title.strip():
                custom_task = text_module._safe_metadata_value(title)
                provenance = "jsonl"
        if claude_family:
            candidate_session_id = obj.get("sessionId")
            if isinstance(candidate_session_id, str) and candidate_session_id:
                session_id = candidate_session_id
            candidate_agent_id = obj.get("agentId")
            if isinstance(candidate_agent_id, str) and candidate_agent_id:
                claude_agent_id = candidate_agent_id
            entrypoint = obj.get("entrypoint")
            if isinstance(entrypoint, str) and entrypoint.strip():
                client = "Claude Code" if entrypoint.strip().lower() == "cli" else text_module._safe_metadata_value(entrypoint)
                provenance = "jsonl"
            top_level_cwd = obj.get("cwd")
            if isinstance(top_level_cwd, str) and top_level_cwd.strip():
                cwd = text_module._safe_metadata_value(top_level_cwd)
                provenance = "jsonl"
            top_level_effort = obj.get("effort")
            if isinstance(top_level_effort, str) and top_level_effort.strip():
                effort = text_module._safe_metadata_value(top_level_effort)
                provenance = "jsonl"
            if kind == "ai-title":
                title = obj.get("aiTitle")
                if isinstance(title, str) and title.strip():
                    generated_task = text_module._safe_metadata_value(title)
                    provenance = "jsonl"
            elif kind == "custom-title":
                title = obj.get("customTitle")
                if isinstance(title, str) and title.strip():
                    custom_task = text_module._safe_metadata_value(title)
                    provenance = "jsonl"
            elif kind == "last-prompt" and task_label == "prompt":
                last_prompt = obj.get("lastPrompt")
                if isinstance(last_prompt, str) and last_prompt.strip():
                    prompt_task = text_module.clip_cells(last_prompt, 160)
        message = obj.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            model = text_module._safe_metadata_value(message["model"])
            provenance = "jsonl"

    task_session_id = session_id
    lineage_namespace: str | None = None
    if claude_family and item.path.parent.name == "subagents":
        parent_session_id = item.path.parent.parent.name
        lineage_namespace = str(item.path.parent.parent.parent)
        session_id = claude_agent_id or item.path.stem.removeprefix("agent-")
    elif claude_family and session_id is None:
        session_id = item.path.stem
        lineage_namespace = str(item.path.parent)
    elif claude_family:
        lineage_namespace = str(item.path.parent)
    elif item.source == "codex":
        lineage_namespace = "codex"

    task = custom_task if custom_task is not None else generated_task or prompt_task or models_module.UNKNOWN
    if custom_task is None and generated_task is None and prompt_task is not None:
        provenance = "jsonl-prompt"
    if (
        claude_family
        and item.profile_id is None
        and task == models_module.UNKNOWN
        and task_session_id is not None
    ):
        task = _claude_registry_name(task_session_id) or models_module.UNKNOWN
    if claude_family and task == models_module.UNKNOWN and task_session_id is not None:
        task = _fallback_task_label(item, cwd, task_session_id)
    if omx_launch:
        client = "OMX / Codex"
    if item.profile_label is not None:
        base_client = "Claude Code" if client == models_module.UNKNOWN else client
        client = text_module.sanitize_terminal_text(f"{base_client} [{item.profile_label}]")
    return models_module.SessionMetadata(
        client, task, model, effort, started, cwd, provenance, agent,
        session_id=text_module._safe_metadata_value(session_id),
        parent_session_id=text_module._safe_metadata_value(parent_session_id),
        lineage_namespace=text_module._safe_metadata_value(lineage_namespace),
        tracking_parent_session_id=text_module._safe_metadata_value(
            tracking_parent_session_id
        ),
    )


def codex_state_database_path() -> Path:
    return config_module.codex_sessions_dir().parent / "state_5.sqlite"


def codex_sqlite_metadata_batch(
    items: Iterable[models_module.ActivityFile], database_path: Path | None = None
) -> dict[tuple[str, str], models_module.SessionMetadata]:
    """Read exact Codex rollout matches through one bounded read-only connection."""
    item_list = list(items)
    results = {target_key(item): models_module.SessionMetadata() for item in item_list}
    if not item_list:
        return results
    path = database_path or codex_state_database_path()
    try:
        database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.15)
        try:
            database.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in database.execute("PRAGMA table_info(threads)")}
            wanted = [name for name in (
                "title", "name", "model", "reasoning_effort", "created_at_ms",
                "created_at", "source", "thread_source", "agent_nickname",
                "agent_role", "cwd",
            ) if name in columns]
            if "rollout_path" not in columns or not wanted:
                return results
            for item in item_list:
                row = database.execute(
                    f"SELECT {', '.join(wanted)} FROM threads WHERE rollout_path = ? LIMIT 1",
                    (str(item.path),),
                ).fetchone()
                if row is None:
                    continue
                values = dict(zip(wanted, row))
                task = values.get("title") or values.get("name")
                client = values.get("thread_source") or values.get("source")
                agent = values.get("agent_nickname") or values.get("agent_role")
                started = _metadata_time(values.get("created_at_ms") or values.get("created_at"))
                results[target_key(item)] = models_module.SessionMetadata(
                    text_module._safe_metadata_value(client), text_module._safe_metadata_value(task),
                    text_module._safe_metadata_value(values.get("model")),
                    text_module._safe_metadata_value(values.get("reasoning_effort")), started,
                    text_module._safe_metadata_value(values.get("cwd")), "codex-state",
                    text_module._safe_metadata_value(agent),
                )
        finally:
            database.close()
    except (OSError, sqlite3.Error):
        return results
    return results


def codex_sqlite_metadata(item: models_module.ActivityFile, database_path: Path | None = None) -> models_module.SessionMetadata:
    return codex_sqlite_metadata_batch([item], database_path)[target_key(item)]


def _merge_metadata(primary: models_module.SessionMetadata, fallback: models_module.SessionMetadata) -> models_module.SessionMetadata:
    scalar_fields = (
        "client", "task", "model", "effort", "started", "cwd", "provenance",
        "agent", "session_id", "parent_session_id", "lineage_namespace",
        "tracking_parent_session_id",
    )
    values = {
        field_name: (
            getattr(primary, field_name)
            if getattr(primary, field_name) not in {models_module.UNKNOWN, None}
            else getattr(fallback, field_name)
        )
        for field_name in scalar_fields
    }
    values["details"] = primary.details or fallback.details
    values["children"] = primary.children or fallback.children
    return models_module.SessionMetadata(**values)


def opencode_metadata(item: models_module.ActivityFile) -> models_module.SessionMetadata:
    """Describe the watched database lineage using bounded, read-only metadata."""
    fallback = models_module.SessionMetadata(client="OpenCode", task=f"{len(item.identities)} lineage seeds")
    if not item.identities:
        return fallback
    try:
        database = sqlite3.connect(item.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.15)
        try:
            deadline = time.monotonic() + 0.2
            database.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            database.execute("PRAGMA query_only = ON")
            columns = {row[1] for row in database.execute("PRAGMA table_info(session)")}
            if not {"id", "parent_id", "title", "time_created", "time_updated"} <= columns:
                return fallback
            optional = [f"substr({name},1,4096)" if name in columns else "NULL"
                        for name in ("directory", "model", "agent")]
            seeds = sorted(item.identities)
            query = f"""WITH RECURSIVE watched(id) AS (
                VALUES {','.join('(?)' for _ in seeds)}
                UNION SELECT s.id FROM session s JOIN watched w ON s.parent_id=w.id
            ) SELECT id, parent_id, substr(title,1,512), time_created, {','.join(optional)}
              FROM session WHERE id IN (SELECT id FROM watched)
              ORDER BY time_updated DESC, id LIMIT 33"""
            sessions = database.execute(query, seeds).fetchall()
            if not sessions:
                return fallback
            message_columns = {row[1] for row in database.execute("PRAGMA table_info(message)")}
            messages_available = {"session_id", "time_created", "id", "data"} <= message_columns
            labels, details, starts, children = [], [], [], []
            for sid, parent_id, title, created, cwd, model_json, agent in sessions[:32]:
                try:
                    model_info = json.loads(model_json) if model_json else {}
                except (ValueError, TypeError):
                    model_info = {}
                if not isinstance(model_info, dict):
                    model_info = {}
                model = text_module._safe_metadata_value(model_info.get("id"))
                effort = text_module._safe_metadata_value(model_info.get("variant"))
                agent = text_module._safe_metadata_value(agent)
                if messages_available:
                    recent = database.execute(
                        "SELECT substr(data,1,65536) FROM message WHERE session_id=? "
                        "ORDER BY time_created DESC, id DESC LIMIT 8", (sid,)
                    ).fetchall()
                    for (raw,) in recent:
                        try:
                            message = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(message, dict) or message.get("role") != "assistant":
                            continue
                        # The session selection reflects a model switch before
                        # the next assistant message has been persisted.
                        if model == models_module.UNKNOWN:
                            model = text_module._safe_metadata_value(message.get("modelID"))
                            effort = text_module._safe_metadata_value(message.get("variant"))
                        agent = text_module._safe_metadata_value(message.get("agent") or agent)
                        break
                label = text_module._safe_metadata_value(title)
                if label == models_module.UNKNOWN:
                    label = _fallback_task_label(item, text_module._safe_metadata_value(cwd), str(sid))
                labels.append(label)
                started = activity_module._timestamp_from_epoch_millis(created)
                if started is not None:
                    starts.append(started)
                children.append(models_module.SessionChildMetadata(
                    session_id=text_module._safe_metadata_value(sid),
                    parent_session_id=text_module._safe_metadata_value(parent_id),
                    task=label, model=model, effort=effort, agent=agent,
                    started=started,
                ))
                details.append(text_module.clip_cells(f"{label} · {sid}", 512))
                details.append(text_module.clip_cells(
                    f"  {model} / {effort} · agent {agent} · started {text_module._local_clock(started)}", 512
                ))
            if len(sessions) > 32:
                details.append("More sessions in this lineage; showing 32 most recently updated.")

            child_ids = {child.session_id for child in children}
            current = next((child for child in children
                            if child.parent_session_id not in child_ids), children[0])
            return models_module.SessionMetadata(
                client="OpenCode", task=" | ".join(labels), model=current.model,
                effort=current.effort, agent=current.agent, started=min(starts, default=None),
                provenance="opencode-state", details=tuple(details),
                lineage_namespace=str(item.path), children=tuple(children),
            )
        finally:
            database.close()
    except (OSError, sqlite3.Error):
        return fallback


def load_session_metadata(item: models_module.ActivityFile, task_label: str = "prompt") -> models_module.SessionMetadata:
    if item.source == "opencode":
        return opencode_metadata(item)
    fallback = (
        jsonl_metadata(item, task_label=task_label)
        if item.source not in {"opencode"} else models_module.SessionMetadata()
    )
    if item.source == "codex":
        result = _merge_metadata(codex_sqlite_metadata(item), fallback)
        if fallback.client != models_module.UNKNOWN:
            result = replace(result, client=fallback.client)
        if result.task == models_module.UNKNOWN:
            short_id = item.path.stem.rsplit("-", 1)[-1][:8]
            result = replace(result, task=_fallback_task_label(item, result.cwd, short_id))
        return result
    if item.source in {"omx", "opencode"} and item.identities:
        label = f"{len(item.identities)} identities" if item.source == "omx" else f"{len(item.identities)} lineage seeds"
        return _merge_metadata(fallback, models_module.SessionMetadata(client=item.source.upper(), task=label))
    return fallback


def external_lineage_registry_path() -> Path:
    """Return the optional explicit cross-provider lineage registry path."""
    return Path.home() / ".config" / "claude-watchdog" / "lineage.json"


def lineage_parent_keys(
    entries: Iterable[
        tuple[
            tuple[str, str],
            models_module.SessionMetadata | models_module.SessionRow,
        ]
    ],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Resolve edges; see test_dashboard_parent_keys_match_legacy_oracle."""
    values = list(entries)
    row_keys: dict[tuple[str, str], list[tuple[str, str]]] = {}
    identities: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for key, value in values:
        row_keys.setdefault(key, []).append(key)
        if value.session_id != models_module.UNKNOWN:
            identities.setdefault(
                (key[0], value.lineage_namespace, value.session_id), []
            ).append(key)

    parents = {}
    for child_key, child_metadata in values:
        parent_keys = identities.get(
            (
                child_key[0],
                child_metadata.lineage_namespace,
                child_metadata.parent_session_id,
            ),
            [],
        )
        if len(parent_keys) == 1 and parent_keys[0] != child_key:
            parents[child_key] = parent_keys[0]
        elif (
            child_metadata.parent_session_id == models_module.UNKNOWN
            and child_metadata.external_parent_key is not None
        ):
            external_parent_keys = row_keys.get(
                child_metadata.external_parent_key, []
            )
            if (
                len(external_parent_keys) == 1
                and external_parent_keys[0] != child_key
            ):
                parents[child_key] = external_parent_keys[0]
    return parents


def apply_omx_tracking_lineage(
    metadata: dict[tuple[str, str], models_module.SessionMetadata],
    fixed_lineage: dict[
        tuple[str, str], models_module.SessionMetadata
    ] | None = None,
) -> dict[tuple[str, str], models_module.SessionMetadata]:
    """Apply OMX candidates without creating cycles through embedded lineage."""
    result = dict(metadata)

    tracking_parent_keys = set()
    for child_key, child_metadata in metadata.items():
        tracking_parent_id = child_metadata.tracking_parent_session_id
        if (
            child_key[0] != "codex"
            or child_metadata.parent_session_id != models_module.UNKNOWN
            or tracking_parent_id == models_module.UNKNOWN
            or tracking_parent_id == child_metadata.session_id
        ):
            continue
        result[child_key] = replace(
            child_metadata, parent_session_id=tracking_parent_id
        )
        tracking_parent_keys.add(child_key)

    guard_values = result
    if fixed_lineage is not None:
        guard_values = {
            key: replace(
                value,
                external_parent_key=fixed_lineage.get(
                    key, value
                ).external_parent_key,
            )
            for key, value in result.items()
        }
    parents = lineage_parent_keys(guard_values.items())

    cyclic = set()
    for child_key in result:
        path, positions = [], {}
        current = child_key
        while current in parents and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        if current in positions:
            cyclic.update(path[positions[current]:])
    for child_key in cyclic & tracking_parent_keys:
        result[child_key] = metadata[child_key]
    return result


def apply_external_lineage_registry(
    metadata: dict[tuple[str, str], models_module.SessionMetadata],
    registry_path: Path | None = None,
) -> dict[tuple[str, str], models_module.SessionMetadata]:
    """Apply exact, bounded external parent declarations to loaded metadata."""
    result = dict(metadata)
    path = registry_path or external_lineage_registry_path()
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return result
        with path.open("rb") as handle:
            raw = handle.read(models_module.MAX_EXTERNAL_LINEAGE_BYTES + 1)
        if len(raw) > models_module.MAX_EXTERNAL_LINEAGE_BYTES:
            return result
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return result
    if not isinstance(document, dict) or document.get("version") != 1:
        return result
    links = document.get("links")
    if not isinstance(links, list) or len(links) > models_module.MAX_EXTERNAL_LINEAGE_LINKS:
        return result

    identities: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key, value in result.items():
        if value.session_id != models_module.UNKNOWN:
            identities.setdefault((key[0], value.session_id), []).append(key)

    declarations: dict[
        tuple[str, str], list[tuple[tuple[str, str], str]]
    ] = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        child, parent, evidence = (
            link.get("child"), link.get("parent"), link.get("evidence")
        )
        if not isinstance(child, dict) or not isinstance(parent, dict):
            continue
        child_ref = (child.get("source"), child.get("session_id"))
        parent_ref = (parent.get("source"), parent.get("session_id"))
        if not all(
            isinstance(source, str) and source in models_module.EXTERNAL_LINEAGE_SOURCES
            and isinstance(session_id, str) and bool(session_id)
            for source, session_id in (child_ref, parent_ref)
        ):
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        declarations.setdefault(child_ref, []).append((parent_ref, evidence))

    for child_ref, declared in declarations.items():
        parent_refs = {parent_ref for parent_ref, _ in declared}
        if len(parent_refs) != 1:
            continue
        child_keys = identities.get(child_ref, [])
        parent_keys = identities.get(next(iter(parent_refs)), [])
        if len(child_keys) != 1 or len(parent_keys) != 1:
            continue
        child_key, parent_key = child_keys[0], parent_keys[0]
        child_metadata = result[child_key]
        if (
            child_key == parent_key
            or child_metadata.parent_session_id != models_module.UNKNOWN
        ):
            continue
        evidence = text_module.sanitize_terminal_text(declared[0][1])
        parent_source, parent_id = next(iter(parent_refs))
        detail = text_module.clip_cells(
            f"external parent {parent_source}:{parent_id} · evidence {evidence}",
            512,
        )
        result[child_key] = replace(
            child_metadata,
            external_parent_key=parent_key,
            details=(*child_metadata.details, detail),
        )
    return result


def load_dashboard_metadata(
    watch_set: Iterable[models_module.ActivityFile], task_label: str = "prompt"
) -> dict[tuple[str, str], models_module.SessionMetadata]:
    items = list(watch_set)
    codex_items = [item for item in items if item.source == "codex"]
    try:
        codex_values = codex_sqlite_metadata_batch(codex_items)
    except Exception:
        codex_values = {}
    values = {}
    for item in items:
        try:
            if item.source == "codex":
                fallback = jsonl_metadata(item, task_label=task_label)
                value = _merge_metadata(
                    codex_values.get(target_key(item), models_module.SessionMetadata()), fallback
                )
                if fallback.client != models_module.UNKNOWN:
                    value = replace(value, client=fallback.client)
                if value.task == models_module.UNKNOWN:
                    short_id = item.path.stem.rsplit("-", 1)[-1][:8]
                    value = replace(value, task=_fallback_task_label(item, value.cwd, short_id))
                values[target_key(item)] = value
            else:
                values[target_key(item)] = load_session_metadata(item, task_label=task_label)
        except Exception:
            values[target_key(item)] = models_module.SessionMetadata()
    fixed_lineage = apply_external_lineage_registry(values)
    return apply_external_lineage_registry(
        apply_omx_tracking_lineage(values, fixed_lineage=fixed_lineage)
    )
