# -*- coding: utf-8 -*-
"""Фоновая очередь нарезки: резка идёт дольше HTTP-запроса, поэтому в фоне."""

from __future__ import annotations

import logging
import queue
import threading
import traceback
from pathlib import Path

from . import db
from .cutter import cut_floor
from .storage import floor_dir

log = logging.getLogger("cutter")
_queue: "queue.Queue[int]" = queue.Queue()
_started = False
_lock = threading.Lock()


def enqueue(floor_id: int) -> None:
    db.execute("UPDATE floors SET status='queued', message='В очереди', updated_at=? "
               "WHERE id=?", (db.now(), floor_id))
    _queue.put(floor_id)
    start_worker()


def start_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_worker, name="cutter", daemon=True).start()


def requeue_pending() -> None:
    """После перезапуска сервиса добираем то, что осталось в очереди."""
    for row in db.query("SELECT id FROM floors WHERE status IN ('queued','working')"):
        _queue.put(row["id"])
    if _queue.qsize():
        start_worker()


def _worker() -> None:
    while True:
        floor_id = _queue.get()
        try:
            _run(floor_id)
        except Exception:                                  # noqa: BLE001
            err = traceback.format_exc(limit=3)
            log.exception("Ошибка нарезки этажа %s", floor_id)
            db.execute("UPDATE floors SET status='error', message=?, log=?, updated_at=? "
                       "WHERE id=?", ("Ошибка обработки", err, db.now(), floor_id))
        finally:
            _queue.task_done()


def _run(floor_id: int) -> None:
    floor = db.one("SELECT f.*, p.name AS project_name, p.id AS pid, "
                   "p.kind AS project_kind "
                   "FROM floors f JOIN projects p ON p.id = f.project_id WHERE f.id=?",
                   (floor_id,))
    if not floor:
        return
    d = floor_dir(floor["pid"], floor_id)
    pdf = d / "source.pdf"
    if not pdf.exists():
        db.execute("UPDATE floors SET status='error', message=?, updated_at=? WHERE id=?",
                   ("Файл не найден", db.now(), floor_id))
        return

    db.execute("UPDATE floors SET status='working', message='Режу…', updated_at=? "
               "WHERE id=?", (db.now(), floor_id))
    log.info("Нарезка: проект %s, этаж %s", floor["project_name"], floor["number"])

    edits = [dict(r) for r in db.query(
        "SELECT action, target, number, polygon FROM floor_edits "
        "WHERE floor_id=? ORDER BY id", (floor_id,))]
    result = cut_floor(pdf, d, floor["project_name"], floor["number"],
                       kind=floor["project_kind"] or "flats", edits=edits)

    db.execute("DELETE FROM apartments WHERE floor_id=?", (floor_id,))
    if result["ok"]:
        for rec in result["apartments"]:
            x0, y0, x1, y1 = rec["box"]
            db.execute(
                "INSERT INTO apartments (floor_id, idx, label, number, filename, "
                "x0, y0, x1, y1) VALUES (?,?,?,?,?,?,?,?,?)",
                (floor_id, rec["idx"], rec["label"], rec["number"], rec["filename"],
                 x0, y0, x1, y1))
        db.execute("UPDATE floors SET status='done', message=?, log=?, updated_at=? "
                   "WHERE id=?", (result["message"], result["log"], db.now(), floor_id))
    else:
        db.execute("UPDATE floors SET status='error', message=?, log=?, updated_at=? "
                   "WHERE id=?", (result["message"], result["log"], db.now(), floor_id))
    log.info("Этаж %s: %s", floor["number"], result["message"])
