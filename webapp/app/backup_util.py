# -*- coding: utf-8 -*-
"""Бэкап/восстановление из админки — без доступа к консоли сервера."""

from __future__ import annotations

import io
import shutil
import zipfile

from .db import DATA_DIR, connect


def make_backup_bytes() -> bytes:
    # WAL: сбрасываем журнал в основной файл базы перед архивацией.
    with connect() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in DATA_DIR.rglob("*"):
            if path.is_file() and "backups" not in path.parts:
                z.write(path, path.relative_to(DATA_DIR))
    return buf.getvalue()


def restore_backup_bytes(data: bytes) -> None:
    """
    Заменяет базу и файлы проектов содержимым архива (лог-файл не трогаем —
    он открыт работающим сервисом). После восстановления сервис стоит перезапустить.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if not any(n.endswith("app.db") for n in names):
            raise ValueError("В архиве нет файла базы данных (app.db) — это не бэкап")

        for wal in ("app.db-wal", "app.db-shm"):
            try:
                (DATA_DIR / wal).unlink(missing_ok=True)
            except OSError:
                pass  # Windows держит файл, пока живо соединение — не критично

        projects_dir = DATA_DIR / "projects"
        if projects_dir.exists():
            shutil.rmtree(projects_dir, ignore_errors=True)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # -wal/-shm — временные файлы SQLite, при бэкапе журнал уже слит в app.db
        # (wal_checkpoint TRUNCATE), их не восстанавливаем: новое соединение
        # создаст свои.
        skip = {"app.db-wal", "app.db-shm"}
        for info in z.infolist():
            if info.filename not in skip:
                z.extract(info, DATA_DIR)
