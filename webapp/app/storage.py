# -*- coding: utf-8 -*-
"""Где лежат файлы. Всё внутри DATA_DIR — этого достаточно для бэкапа."""

from __future__ import annotations

from pathlib import Path

from .db import DATA_DIR

FILES = DATA_DIR / "projects"


def project_dir(project_id: int) -> Path:
    return FILES / str(project_id)


def floor_dir(project_id: int, floor_id: int) -> Path:
    return project_dir(project_id) / "floors" / str(floor_id)


def apartments_dir(project_id: int, floor_id: int) -> Path:
    return floor_dir(project_id, floor_id) / "apartments"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
