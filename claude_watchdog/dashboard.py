"""Interactive terminal dashboard rendering."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

from . import activity as activity_module
from . import metadata as metadata_module
from . import models as models_module
from . import presentation as presentation_module
from . import reporting as reporting_module
from . import text as text_module

def resolve_display(requested: str, stdin=None, stdout=None) -> str:
    if requested != "auto":
        return requested
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    term = os.environ.get("TERM", "")
    try:
        capable = stdin.isatty() and stdout.isatty() and bool(term) and term.lower() != "dumb"
    except (AttributeError, OSError):
        capable = False
    return "dashboard" if capable else "log"


def make_dashboard_snapshot(
    now: datetime,
    cfg: models_module.Config,
    watch_set: list[models_module.ActivityFile],
    activity: list[tuple[models_module.ActivityFile, datetime]],
    user_idle: float,
    next_poll_seconds: float,
    metadata: dict[tuple[str, str], models_module.SessionMetadata],
    admission_notice: str = "",
) -> models_module.DashboardSnapshot:
    timestamps = {metadata_module.target_key(item): timestamp for item, timestamp in activity}
    rows = []
    for item in watch_set:
        key = metadata_module.target_key(item)
        meta = metadata.get(key, models_module.SessionMetadata())
        timestamp = timestamps.get(key)
        age = activity_module._activity_age(now, timestamp) if timestamp else None
        remaining = max(0.0, cfg.idle_seconds - age) if age is not None else 0.0
        rows.append(models_module.SessionRow(
            key, item.source, meta.client, meta.task, meta.model, meta.effort,
            meta.started, timestamp, remaining, str(item.path), len(item.identities),
            meta.provenance, age is not None and age < cfg.idle_seconds, meta.agent,
            meta.details, meta.session_id, meta.parent_session_id,
            meta.lineage_namespace, meta.children,
            external_parent_key=meta.external_parent_key,
        ))
    holding = sum(row.holding for row in rows)
    return models_module.DashboardSnapshot(
        now, tuple(rows), len(watch_set), holding, holding == 0, user_idle,
        cfg.user_idle_seconds, next_poll_seconds, cfg.source,
        cfg.session_discovery, cfg.idle_seconds, admission_notice,
    )


def _dashboard_parent_keys(
    rows: Iterable[models_module.SessionRow],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Resolve safe, provider-local parent links for visible dashboard rows."""
    row_list = list(rows)
    parents = metadata_module.lineage_parent_keys(
        (row.key, row) for row in row_list
    )

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


def _dashboard_sort_key(row: models_module.SessionRow, sort: str):
    if sort == "title":
        return (row.task.casefold(), row.source, row.path)
    if sort == "source":
        return (row.source, row.task.casefold(), row.path)
    return (
        row.last_event is None,
        -(row.last_event.timestamp() if row.last_event else 0),
        row.path,
    )


def _expanded_dashboard_rows(rows: Iterable[models_module.SessionRow]) -> list[models_module.SessionRow]:
    """Expand grouped metadata into descriptive rows without adding activity guards."""
    expanded = []
    for row in rows:
        if not row.children:
            expanded.append(row)
            continue
        namespace = row.lineage_namespace if row.lineage_namespace != models_module.UNKNOWN else row.path
        group_id = f"watchdog-group:{row.path}"
        expanded.append(replace(row, session_id=group_id, lineage_namespace=namespace))
        child_ids = {child.session_id for child in row.children}
        for child in row.children:
            parent_id = (
                child.parent_session_id
                if child.parent_session_id in child_ids else group_id
            )
            expanded.append(models_module.SessionRow(
                key=(row.source, f"{row.path}#session={child.session_id}"),
                source=row.source, client=row.client, task=child.task,
                model=child.model, effort=child.effort, started=child.started,
                last_event=None, quiet_remaining=0.0, path=row.path,
                identity_count=0, provenance=row.provenance, holding=False,
                agent=child.agent, session_id=child.session_id,
                parent_session_id=parent_id, lineage_namespace=namespace,
                display_only=True,
            ))
    return expanded


def dashboard_tree_prefixes(
    rows: Iterable[models_module.SessionRow],
) -> dict[tuple[str, str], str]:
    """Return pstree-style prefixes for already ordered visible rows."""
    row_list = list(rows)
    parents = _dashboard_parent_keys(row_list)
    children: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in row_list:
        parent = parents.get(row.key)
        if parent is not None:
            children.setdefault(parent, []).append(row.key)
    prefixes = {}
    for row in row_list:
        if row.key not in parents:
            prefixes[row.key] = ""
            continue
        lineage = []
        current = row.key
        while current in parents:
            parent = parents[current]
            siblings = children.get(parent, [])
            lineage.append(current == siblings[-1] if siblings else True)
            current = parent
        lineage.reverse()
        prefixes[row.key] = "".join(
            ("   " if is_last else "│  ") if index < len(lineage) - 1
            else ("└─ " if is_last else "├─ ")
            for index, is_last in enumerate(lineage)
        )
    return prefixes


def visible_dashboard_rows(rows: Iterable[models_module.SessionRow], state: models_module.DashboardState) -> list[models_module.SessionRow]:
    query = state.query.casefold()
    visible = [row for row in _expanded_dashboard_rows(rows) if (
        state.source_filter is None or row.source == state.source_filter
    ) and (
        not query or query in " ".join((row.source, row.client, row.task, row.model, row.effort)).casefold()
    )]
    visible.sort(key=lambda row: _dashboard_sort_key(row, state.sort))
    if not state.tree:
        return visible

    parents = _dashboard_parent_keys(visible)
    children: dict[tuple[str, str], list[models_module.SessionRow]] = {}
    for row in visible:
        parent = parents.get(row.key)
        if parent is not None:
            children.setdefault(parent, []).append(row)
    ordered, visited = [], set()

    def append_subtree(root):
        pending = [root]
        while pending:
            row = pending.pop()
            if row.key in visited:
                continue
            visited.add(row.key)
            ordered.append(row)
            pending.extend(reversed(children.get(row.key, [])))

    for row in visible:
        if row.key not in parents:
            append_subtree(row)
    for row in visible:
        append_subtree(row)
    return ordered


def retain_dashboard_selection(state: models_module.DashboardState, rows: list[models_module.SessionRow]) -> None:
    if not rows:
        state.selected = state.scroll = 0
        state.selected_key = None
        return
    if state.selected_key is not None:
        for index, row in enumerate(rows):
            if row.key == state.selected_key:
                state.selected = index
                break
    state.selected = min(max(0, state.selected), len(rows) - 1)
    state.selected_key = rows[state.selected].key


def handle_dashboard_key(state: models_module.DashboardState, key: int | str, row_count: int) -> str | None:
    if key in {-1, None}:
        return None
    code = ord(key) if isinstance(key, str) and len(key) == 1 else key
    if state.filter_input:
        if code == 27:
            state.query = state.query_before_edit
            state.filter_input = False
        elif code in {10, 13}:
            state.filter_input = False
        elif code in {8, 127, 263}:
            state.query = state.query[:-1]
        elif isinstance(key, str) and len(key) == 1 and key.isprintable():
            state.query += key
        elif isinstance(code, int) and 32 <= code < 256 and chr(code).isprintable():
            state.query += chr(code)
        return "filter"
    if code in {ord("q"), 3}:
        raise KeyboardInterrupt
    if code == ord("/"):
        state.query_before_edit = state.query
        state.filter_input = True
        return "filter"
    if code in {ord("p"), ord("f")}:
        choices = (None, *models_module.ALL_SOURCES)
        try:
            state.source_filter = choices[(choices.index(state.source_filter) + 1) % len(choices)]
        except ValueError:
            state.source_filter = None
    elif code == ord("s"):
        state.sort = models_module.SORT_CHOICES[(models_module.SORT_CHOICES.index(state.sort) + 1) % len(models_module.SORT_CHOICES)]
    elif code == ord("t"):
        state.tree = not state.tree
    elif code == ord("c"):
        state.query = ""
        state.source_filter = None
    elif code in {ord("j"), 258} and row_count:
        state.selected = min(row_count - 1, state.selected + 1)
        state.selected_key = None
    elif code in {ord("k"), 259} and row_count:
        state.selected = max(0, state.selected - 1)
        state.selected_key = None
    elif code in {10, 13}:
        state.details = not state.details
    return None


class TerminalDashboard:
    SOURCE_COLORS = presentation_module.SOURCE_COLORS
    LABEL_COLORS = presentation_module.LABEL_COLORS

    def __init__(self, screen, cfg: models_module.Config, curses_module=None):
        self.screen = screen
        self.cfg = cfg
        self.curses = curses_module
        self.state = models_module.DashboardState()
        self.snapshot: models_module.DashboardSnapshot | None = None
        self.history = reporting_module.WatchHistory()
        self._color_pairs: dict[str, int] = {}
        self._label_palette: list[int] = []
        self._label_colors: dict[str, dict[str, int]] = {"client": {}, "model": {}}
        self._configure_colors()

    def _configure_colors(self) -> None:
        if not presentation_module.colors_enabled(self.cfg) or self.curses is None:
            return
        try:
            if not self.curses.has_colors():
                return
            self.curses.start_color()
            background = -1
            try:
                self.curses.use_default_colors()
            except Exception:
                background = 0
            palette = self.LABEL_COLORS if self.curses.COLORS >= 256 else (6, 5, 2, 3, 4, 1)
            foregrounds = list(dict.fromkeys((*self.SOURCE_COLORS.values(), *palette)))
            attributes = {}
            for pair, color in enumerate(foregrounds[:max(0, self.curses.COLOR_PAIRS - 1)], 1):
                self.curses.init_pair(pair, color, background)
                attributes[color] = self.curses.color_pair(pair)
            self._color_pairs = {source: attributes.get(color, 0) for source, color in self.SOURCE_COLORS.items()}
            self._label_palette = [attributes[color] for color in palette if color in attributes]
        except Exception:
            self._color_pairs.clear()
            self._label_palette.clear()

    def _label_attr(self, category: str, value: str) -> int:
        if value == models_module.UNKNOWN or not self._label_palette:
            return 0
        assigned = self._label_colors[category]
        if value not in assigned:
            assigned[value] = self._label_palette[len(assigned) % len(self._label_palette)]
        return assigned[value]

    def _put(self, y: int, text: str, *, attr=0, x=0) -> None:
        height, width = self.screen.getmaxyx()
        if not (0 <= y < height) or not (0 <= x < width - 1):
            return
        layout = text_module._sanitize_terminal_chars(text, collapse_spacing=False)
        safe = text_module._clip_text_cells(layout, width - x - 1)
        try:
            self.screen.addnstr(y, x, safe, width - x - 1, attr)
        except Exception:
            pass

    def update(self, snapshot: models_module.DashboardSnapshot) -> None:
        self.snapshot = snapshot
        # Assign before filtering so hiding a row cannot change another label's color.
        for row in snapshot.rows:
            self._label_attr("client", row.client)
            self._label_attr("model", row.model)
        rows = visible_dashboard_rows(snapshot.rows, self.state)
        tree_prefixes = dashboard_tree_prefixes(rows) if self.state.tree else {}
        retain_dashboard_selection(self.state, rows)
        height, width = self.screen.getmaxyx()
        try:
            self.screen.erase()
        except Exception:
            return
        state_word = "AWAKE" if snapshot.holding_count else "SESSIONS QUIET"
        self._put(0, f"CLAUDE WATCHDOG  {state_word} · {snapshot.holding_count} holding / {snapshot.watched_count} targets   next scan {text_module._duration(snapshot.next_poll_seconds)}   {snapshot.discovery} · {snapshot.source}")
        gate = (
            f"session quiet {int(snapshot.idle_seconds // 60)}m · user idle pending (checked after sessions quiet)"
            if snapshot.holding_count else
            f"sessions quiet · user idle {int(snapshot.user_idle or 0)}s / {int(snapshot.user_idle_required)}s"
        )
        self._put(1, gate)
        body_start = 3
        footer_lines = 3
        # Reserve source, client, age, countdown and separators. Give model
        # labels enough room before assigning the rest to task descriptions.
        client_width = 14
        model_width = 20
        task_width = 35
        if width >= 100:
            desired_client = max(14, max((text_module.text_cells(row.client) for row in rows), default=14))
            extra = max(0, width - 34 - 14 - 20 - 16)
            client_width = 14 + min(extra, desired_client - 14)
            extra -= client_width - 14
            desired_model = max((text_module.text_cells(row.model if row.effort == models_module.UNKNOWN else
                                           f"{row.model} / {row.effort}") for row in rows), default=20)
            model_width = 20 + min(extra, max(0, desired_model - 20))
            task_width = width - 34 - client_width - model_width
        elif width >= 80:
            task_width = width - 48
        model_x = 14 + client_width + task_width if width >= 100 else 13 + task_width
        if width >= 100:
            self._put(2, f"  {text_module.pad_cells('SOURCE', 9)} {text_module.pad_cells('CLIENT', client_width)} "
                      f"{text_module.pad_cells('TASK / AGENT TREE' if self.state.tree else 'TASK', task_width)} {text_module.pad_cells('MODEL / EFFORT', model_width)} "
                      f"{'LAST':>7} {'QUIET IN':>10}")
        elif width >= 80:
            self._put(2, f"  {text_module.pad_cells('SOURCE', 9)} {text_module.pad_cells('TASK', task_width)} "
                      f"{text_module.pad_cells('MODEL / EFFORT', model_width)} {'LAST':>6} {'QUIET':>6}")
        else:
            self._put(2, "  SOURCE · TASK                         LAST · QUIET")
        detail_lines = []
        if self.state.details and rows:
            available = max(0, height - body_start - footer_lines - 3)
            detail_lines = list(rows[self.state.selected].details[:min(8, available)])
            if detail_lines and len(rows[self.state.selected].details) > len(detail_lines):
                detail_lines[-1] = "More session details available in the final run report."
        capacity = max(0, height - body_start - footer_lines - (2 if self.state.details else 0) - len(detail_lines))
        if self.state.selected < self.state.scroll:
            self.state.scroll = self.state.selected
        if self.state.selected >= self.state.scroll + max(1, capacity):
            self.state.scroll = self.state.selected - capacity + 1
        shown = rows[self.state.scroll:self.state.scroll + capacity]
        if not shown and capacity:
            self._put(body_start, "No watched sessions match this filter")
        for offset, row in enumerate(shown):
            index = self.state.scroll + offset
            marker = "▸" if index == self.state.selected else " "
            last = text_module._relative_age(snapshot.now, row.last_event)
            current_age = (
                max(0.0, (snapshot.now - row.last_event).total_seconds())
                if row.last_event is not None else None
            )
            displayed_remaining = (
                max(0.0, snapshot.idle_seconds - current_age)
                if current_age is not None else 0.0
            )
            quiet = text_module._duration(displayed_remaining) if row.holding else "quiet"
            if row.display_only:
                last = quiet = "group"
            model = row.model if row.effort == models_module.UNKNOWN else f"{row.model} / {row.effort}"
            task_text = tree_prefixes.get(row.key, "") + text_module.sanitize_terminal_text(row.task)
            task_text = text_module._clip_text_cells(task_text, task_width)
            task_cell = task_text + " " * max(0, task_width - text_module.text_cells(task_text))
            if width >= 100:
                line = (
                    f"{marker} {text_module.pad_cells(row.source.upper(), 9)} "
                    f"{text_module.pad_cells(row.client, client_width)} {task_cell} "
                    f"{text_module.pad_cells(model, model_width)} {last:>7} {quiet:>10}"
                )
            elif width >= 80:
                line = (
                    f"{marker} {text_module.pad_cells(row.source.upper(), 9)} "
                    f"{task_cell} {text_module.pad_cells(model, 20)} "
                    f"{last:>6} {quiet:>6}"
                )
            else:
                fixed_width = text_module.text_cells(f"{marker} {row.source.upper()} ·    {last} · {quiet}")
                task = text_module._clip_text_cells(tree_prefixes.get(row.key, "") + text_module.sanitize_terminal_text(row.task),
                                        max(4, width - fixed_width - 1))
                line = f"{marker} {row.source.upper()} · {task}   {last} · {quiet}"
            attr = 0
            if index == self.state.selected and self.curses is not None:
                attr |= getattr(self.curses, "A_REVERSE", 0)
            self._put(body_start + offset, line, attr=attr)
            if self._color_pairs:
                self._put(body_start + offset, row.source.upper(), x=2,
                          attr=attr | self._color_pairs.get(row.source, 0))
                if width >= 100:
                    self._put(body_start + offset, text_module.pad_cells(row.client, client_width), x=12,
                              attr=attr | self._label_attr("client", row.client))
                if width >= 80:
                    self._put(body_start + offset, text_module.pad_cells(model, model_width), x=model_x,
                              attr=attr | self._label_attr("model", row.model))
        footer_y = max(body_start, height - footer_lines)
        if self.state.details and rows and footer_y >= 2:
            row = rows[self.state.selected]
            started = text_module._local_clock(row.started)
            event = text_module._local_clock(row.last_event)
            agent = f" · agent {row.agent}" if row.agent != models_module.UNKNOWN else ""
            self._put(footer_y - 2 - len(detail_lines), f"selected: started {started} · event {event} · {row.client}{agent} · {row.model} / {row.effort}")
            parent = f" · parent {row.parent_session_id}" if row.parent_session_id != models_module.UNKNOWN else ""
            identity = f" · session {row.session_id}" if row.session_id != models_module.UNKNOWN else ""
            scope = " · timing belongs to database guard" if row.display_only else ""
            self._put(footer_y - 1 - len(detail_lines), f"metadata {row.provenance}{identity}{parent}{scope} · path {row.path}")
            for index, detail in enumerate(detail_lines):
                self._put(footer_y - len(detail_lines) + index, detail)
        prompt = (
            f"filter: {self.state.query}_"
            if self.state.filter_input else
            "q exit safely  / filter  p/f provider  s sort  t tree/flat  ↑↓/jk move  Enter details  c clear"
        )
        self._put(footer_y, prompt)
        provider = self.state.source_filter or "all"
        notice = f" · + {snapshot.admission_notice}" if snapshot.admission_notice else ""
        self._put(
            footer_y + 1,
            f"showing {len(rows)} rows · {snapshot.watched_count} watch targets · provider {provider} · "
            f"{'tree' if self.state.tree else 'flat'} · sort {self.state.sort} · task {self.cfg.task_label}{notice}",
        )
        self._put(footer_y + 2, "persisted activity, not process liveness")
        try:
            self.screen.refresh()
        except Exception:
            pass

    def process_input(self) -> None:
        get_wch = getattr(self.screen, "get_wch", None)
        try:
            key = get_wch() if get_wch is not None else self.screen.getch()
        except Exception as exc:
            curses_error = getattr(self.curses, "error", None)
            if isinstance(curses_error, type) and isinstance(exc, curses_error):
                key = -1
            else:
                raise
        rows = visible_dashboard_rows(self.snapshot.rows, self.state) if self.snapshot else []
        handle_dashboard_key(self.state, key, len(rows))
        if self.snapshot is not None and key != -1:
            self.update(self.snapshot)

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        last_second = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.process_input()
            second = int(remaining)
            if self.snapshot is not None and second != last_second:
                self.update(replace(self.snapshot, next_poll_seconds=remaining, now=datetime.now(timezone.utc)))
                last_second = second
            time.sleep(min(0.1, remaining))


def _import_curses():
    import curses
    return curses


def _dashboard_sigterm(signum, frame) -> None:
    raise KeyboardInterrupt


@contextmanager
def dashboard_context(cfg: models_module.Config):
    curses = None
    screen = None
    removed: list[tuple[int, logging.Handler]] = []
    prior_sigterm = None
    try:
        curses = _import_curses()
        if signal.getsignal(signal.SIGTERM) is not signal.SIG_IGN:
            prior_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _dashboard_sigterm)
        screen = curses.initscr()
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)
        screen.nodelay(True)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        for index, handler in reversed(list(enumerate(models_module.log.handlers))):
            if getattr(handler, "stream", None) is sys.stdout:
                removed.append((index, handler))
                models_module.log.removeHandler(handler)
        yield TerminalDashboard(screen, cfg, curses)
    except KeyboardInterrupt:
        raise
    except models_module.WatchdogError:
        raise
    except Exception as exc:
        raise models_module.WatchdogError(f"terminal dashboard unavailable: {exc}") from exc
    finally:
        cleanup_interrupt = None
        cleanup_error = None
        if screen is not None:
            for cleanup in (
                lambda: screen.keypad(False),
                curses.nocbreak,
                curses.echo,
                curses.endwin,
            ):
                try:
                    cleanup()
                except KeyboardInterrupt as exc:
                    cleanup_interrupt = cleanup_interrupt or exc
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
        try:
            for index, handler in sorted(removed):
                models_module.log.handlers.insert(min(index, len(models_module.log.handlers)), handler)
        except KeyboardInterrupt as exc:
            cleanup_interrupt = cleanup_interrupt or exc
        if prior_sigterm is not None:
            while True:
                try:
                    signal.signal(signal.SIGTERM, prior_sigterm)
                    break
                except KeyboardInterrupt as exc:
                    cleanup_interrupt = cleanup_interrupt or exc
        if cleanup_interrupt is not None:
            raise cleanup_interrupt
        if cleanup_error is not None:
            raise models_module.TerminalRestoreError(f"unable to restore terminal: {cleanup_error}") from cleanup_error


def dashboard_lines(
    snapshot: models_module.DashboardSnapshot,
    state: models_module.DashboardState | None = None,
    *,
    width: int = 120,
    height: int = 24,
) -> list[str]:
    """Render a deterministic, terminal-free frame for tests and visual review."""
    class FrameScreen:
        def __init__(self):
            self.lines: dict[int, str] = {}

        def getmaxyx(self):
            return height, width

        def erase(self):
            self.lines.clear()

        def addnstr(self, y, x, text, count, *attrs):
            self.lines[y] = text[:count]

        def refresh(self):
            pass

    screen = FrameScreen()
    dashboard = TerminalDashboard(screen, models_module.Config(no_color=True))
    dashboard.state = state or models_module.DashboardState()
    dashboard.update(snapshot)
    return [screen.lines.get(y, "") for y in range(height)]
