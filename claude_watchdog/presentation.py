"""Shared color and presentation policy."""

from __future__ import annotations

import os

from . import models as models_module

SOURCE_COLORS = {"claude": 3, "codex": 6, "omx": 5, "opencode": 2}
LABEL_COLORS = (81, 213, 114, 221, 147, 208, 45, 177, 156, 204, 180, 117)

def colors_enabled(cfg: models_module.Config) -> bool:
    return not cfg.no_color and "NO_COLOR" not in os.environ
