"""Shared runtime data, errors, and constants."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


LOG_FILE = Path.home() / "claude-watchdog.log"


READ_CHUNK_BYTES = 64 * 1024


MAX_METADATA_BYTES = 256 * 1024


MAX_CLAUDE_REGISTRY_FILES = 512


MAX_CLAUDE_REGISTRY_BYTES = 2 * 1024 * 1024


MAX_OMX_SESSION_BYTES = READ_CHUNK_BYTES


MAX_PROCESS_ANCESTRY_HOPS = 64


MAX_PROCESS_TABLE_ROWS = 65_536


PROCESS_PROBE_TIMEOUT_SECONDS = 1.0


CLAUDE_PROCESS_START_TOLERANCE_SECONDS = 0.0


OMX_PROCESS_START_TOLERANCE_SECONDS = 120.0


MAX_EXTERNAL_LINEAGE_BYTES = 256 * 1024


MAX_EXTERNAL_LINEAGE_LINKS = 256


MAX_RETAINED_PROCESS_LINEAGE_LINKS = MAX_EXTERNAL_LINEAGE_LINKS


MAX_OMX_TRACKING_BYTES = 256 * 1024


MAX_OMX_TRACKING_SESSIONS = 256


MAX_OMX_TRACKING_THREADS = 256


MAX_PROFILES_BYTES = 64 * 1024


MAX_CLAUDE_PROFILES = 32


MAX_SYNTHESIZED_ANCESTORS = 32


EXTERNAL_LINEAGE_SOURCES = ("claude", "codex")


MAX_FUTURE_SKEW_SECONDS = 300


SOURCE_CHOICES = (
    "auto",
    "claude",
    "codex",
    "omx",
    "opencode",
    "codex-omx",
)


ALL_SOURCES = ("claude", "codex", "omx", "opencode")


OMX_ID_FIELDS = ("session_id", "native_session_id", "thread_id")


DISPLAY_CHOICES = ("auto", "dashboard", "log")


TASK_LABEL_CHOICES = ("metadata", "prompt")


SORT_CHOICES = ("recent", "title", "source")


UNKNOWN = "unknown"


log = logging.getLogger("claude-watchdog")


class WatchdogError(RuntimeError):
    """An operational error that must prevent a forced sleep."""


class ActivityReadError(WatchdogError):
    """Persisted activity could not be read safely."""


class PresenceCheckError(WatchdogError):
    """The macOS user-idle state could not be determined."""


class PowerCommandError(WatchdogError):
    """A macOS power command failed."""


class TerminalRestoreError(WatchdogError):
    """Terminal cleanup failed; falling back to log mode is unsafe."""


class OpenCodeDatabaseError(WatchdogError):
    """The OpenCode activity database could not be read safely."""


@dataclass(frozen=True)
class ClaudeProfile:
    id: str
    label: str
    projects_dir: Path


@dataclass(frozen=True)
class ActivityFile:
    path: Path
    source: str
    snapshot_size: int | None = None
    identities: frozenset[str] = frozenset()
    profile_id: str | None = None
    profile_label: str | None = None

    @property
    def label(self) -> str:
        source = (
            f"{self.source}[{self.profile_label}]"
            if self.profile_label is not None else self.source
        )
        return f"{source}:{self.path}"


@dataclass
class Config:
    idle_minutes: float = 30.0
    user_idle_minutes: float = 5.0  # 0 disables the user-idle gate.
    select_window_seconds: float = 900.0
    poll_seconds: float = 60.0
    source: str = "auto"
    session_discovery: str = "live"
    dry_run: bool = False
    display: str = "auto"
    no_color: bool = False
    task_label: str = "prompt"
    claude_profiles: tuple[ClaudeProfile, ...] = ()
    profiles_file: Path | None = None

    @property
    def idle_seconds(self) -> float:
        return self.idle_minutes * 60

    @property
    def user_idle_seconds(self) -> float:
        return self.user_idle_minutes * 60


@dataclass(frozen=True)
class SessionChildMetadata:
    session_id: str
    parent_session_id: str = UNKNOWN
    task: str = UNKNOWN
    model: str = UNKNOWN
    effort: str = UNKNOWN
    agent: str = UNKNOWN
    started: datetime | None = None


@dataclass(frozen=True)
class SessionMetadata:
    client: str = UNKNOWN
    task: str = UNKNOWN
    model: str = UNKNOWN
    effort: str = UNKNOWN
    started: datetime | None = None
    cwd: str = UNKNOWN
    provenance: str = UNKNOWN
    agent: str = UNKNOWN
    details: tuple[str, ...] = ()
    session_id: str = UNKNOWN
    parent_session_id: str = UNKNOWN
    lineage_namespace: str = UNKNOWN
    children: tuple[SessionChildMetadata, ...] = ()
    external_parent_key: tuple[str, str] | None = None
    tracking_parent_session_id: str = UNKNOWN


@dataclass(frozen=True)
class SessionRow:
    key: tuple[str, str]
    source: str
    client: str
    task: str
    model: str
    effort: str
    started: datetime | None
    last_event: datetime | None
    quiet_remaining: float
    path: str
    identity_count: int
    provenance: str
    holding: bool
    agent: str = UNKNOWN
    details: tuple[str, ...] = ()
    session_id: str = UNKNOWN
    parent_session_id: str = UNKNOWN
    lineage_namespace: str = UNKNOWN
    children: tuple[SessionChildMetadata, ...] = ()
    display_only: bool = False
    lineage_context_only: bool = False
    external_parent_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class DashboardSnapshot:
    now: datetime
    rows: tuple[SessionRow, ...]
    watched_count: int
    holding_count: int
    session_quiet: bool
    user_idle: float | None
    user_idle_required: float
    next_poll_seconds: float
    source: str
    discovery: str
    idle_seconds: float
    admission_notice: str = ""
    display_rows: tuple[SessionRow, ...] = ()


@dataclass
class DashboardState:
    query: str = ""
    source_filter: str | None = None
    sort: str = "recent"
    selected: int = 0
    selected_key: tuple[str, str] | None = None
    scroll: int = 0
    details: bool = False
    filter_input: bool = False
    query_before_edit: str = ""
    tree: bool = True
