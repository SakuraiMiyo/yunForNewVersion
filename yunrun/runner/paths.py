# -*- coding: utf-8 -*-
"""Path resolution for the runner, after the layout cleanup.

The runner used to sit at the project root, so several modules derived paths with
``dirname(dirname(__file__))`` or treated their own directory as the project root. Once
the runner moved into ``runner/``, those assumptions silently pointed one level too deep
(``runner/config.ini`` instead of ``config.ini``) -- silent because every caller in this
repo passes absolute paths, so only the fallback paths broke.

Keep the rules in one place:

    PROJECT_ROOT  -- the directory holding config.ini and tasks_* (runner/..)
    RUNNER_DIR    -- the directory holding main.py / yun_http.py / yun_face.py
    TOOLS_DIR     -- the runner's own tools (drift, Login, proxy, ...)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RUNNER_DIR.parent
TOOLS_DIR = RUNNER_DIR / "tools"


def ensure_importable() -> None:
    """Put the runner, its tools and the project root on sys.path.

    The runner's modules import each other by bare name (``import yun_http``) and the
    tools import ``tools.drift``, so both directories must be importable.
    """
    for path in (str(RUNNER_DIR), str(PROJECT_ROOT), str(TOOLS_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)


def project_path(*parts: str) -> str:
    """Absolute path under the project root (where config.ini and tasks_* live)."""
    return os.path.normpath(os.path.join(str(PROJECT_ROOT), *parts))


def default_config() -> str:
    return project_path("config.ini")


def default_tasks_dir() -> str:
    return project_path("tasks_fch")


# Importing this module should be enough to make the runner importable: every caller in
# this package relies on bare-name imports working.
ensure_importable()
