"""Pure terminal text and display-time helpers."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

from . import models as models_module

def _sanitize_terminal_chars(value: object, *, collapse_spacing: bool) -> str:
    if not isinstance(value, str):
        return models_module.UNKNOWN
    text = unicodedata.normalize("NFC", value)
    cleaned: list[str] = []
    for char in text:
        code = ord(char)
        category = unicodedata.category(char)
        if char in "\r\n\t":
            cleaned.append(" ")
        elif code < 32 or 127 <= code <= 159 or category in {"Cf", "Cs"}:
            continue
        else:
            cleaned.append(char)
    result = "".join(cleaned)
    if collapse_spacing:
        result = re.sub(r"\s+", " ", result).strip()
    return result or models_module.UNKNOWN


def sanitize_terminal_text(value: object) -> str:
    """Make local metadata safe and compact enough for one terminal cell."""
    return _sanitize_terminal_chars(value, collapse_spacing=True)


def text_cells(value: str) -> int:
    width = 0
    for char in value:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _clip_text_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if text_cells(text) <= width:
        return text
    if width == 1:
        return "…"
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if used + char_width > width - 1:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def clip_cells(value: object, width: int) -> str:
    return _clip_text_cells(sanitize_terminal_text(value), width)


def pad_cells(value: object, width: int) -> str:
    """Clip and right-pad a metadata value to an exact terminal-cell width."""
    clipped = clip_cells(value, width)
    return clipped + " " * max(0, width - text_cells(clipped))


def _safe_metadata_value(value: object) -> str:
    return sanitize_terminal_text(value) if isinstance(value, str) and value.strip() else models_module.UNKNOWN


def _relative_age(now: datetime, value: datetime | None) -> str:
    if value is None:
        return models_module.UNKNOWN
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _duration(value: float) -> str:
    seconds = max(0, int(value))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _local_clock(value: datetime | None) -> str:
    if value is None:
        return models_module.UNKNOWN
    try:
        return value.astimezone().strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return models_module.UNKNOWN


def _report_clock(value: datetime | None) -> str:
    if value is None:
        return models_module.UNKNOWN
    try:
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (OverflowError, OSError, ValueError):
        return models_module.UNKNOWN


def _quiet_deadline(value: datetime | None, idle_seconds: float) -> datetime | None:
    try:
        return value + timedelta(seconds=idle_seconds) if value is not None else None
    except OverflowError:
        return None


def _report_duration(seconds: float) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return " ".join(f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (seconds, "s")) if value) or "0s"
