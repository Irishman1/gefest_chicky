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
_worker_thread: threading.Thread | None = None
_lock = threading.Lock()


def enqueue(floor_id: int) -> None:
    db.execute("UPDATE floors SET status='queued', message='В очереди', updated_at=? "
               "WHERE id=?", (db.now(), floor_id))
    _queue.put(floor_id)
    start_worker()


def start_worker() -> None:
    """Поднимает воркер, если его нет или он больше не жив.

    Раньше здесь стоял флаг «уже запускали». Если поток по любой причине
    умирал, флаг оставался поднятым, и новый воркер не запускался уже никогда:
    все следующие этажи навсегда оставались «В очереди», а страница крутила
    загрузку. Живость потока — единственный надёжный признак.
    """
    global _worker_thread
    with _lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_worker, name="cutter",
                                          daemon=True)
        _worker_thread.start()


def requeue_pending() -> None:
    """После перезапуска сервиса добираем то, что осталось в очереди.

    Этаж со статусом 'working' резался в момент остановки контейнера — его
    надо начать заново, а не считать готовым.
    """
    rows = db.query("SELECT id FROM floors WHERE status IN ('queued','working')")
    for row in rows:
        db.execute("UPDATE floors SET status='queued', message='В очереди', "
                   "updated_at=? WHERE id=?", (db.now(), row["id"]))
        _queue.put(row["id"])
    if rows:
        log.info("После перезапуска возвращено в очередь этажей: %s", len(rows))
        start_worker()


def _worker() -> None:
    """Крутится, пока жив процесс. Из этого цикла нельзя выпасть.

    Ошибка при записи самой ошибки в базу (например, заблокированный SQLite)
    раньше пробивала except наружу, поток тихо умирал, и очередь вставала
    навсегда. Поэтому наружу не выпускаем ничего.
    """
    while True:
        floor_id = _queue.get()
        try:
            _run(floor_id)
        except Exception:                                  # noqa: BLE001
            err = traceback.format_exc(limit=3)
            log.exception("Ошибка нарезки этажа %s", floor_id)
            try:
                db.execute("UPDATE floors SET status='error', message=?, log=?, "
                           "updated_at=? WHERE id=?",
                           ("Ошибка обработки", err, db.now(), floor_id))
            except Exception:                              # noqa: BLE001
                log.exception("Не удалось записать ошибку этажа %s", floor_id)
        finally:
            try:
                _queue.task_done()
            except Exception:                              # noqa: BLE001
                log.exception("task_done для этажа %s", floor_id)


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
        status = "review" if result.get("needs_review") else "done"
        db.execute("UPDATE floors SET status=?, message=?, log=?, updated_at=? "
                   "WHERE id=?", (status, result["message"], result["log"], db.now(), floor_id))
    else:
        db.execute("UPDATE floors SET status='error', message=?, log=?, updated_at=? "
                   "WHERE id=?", (result["message"], result["log"], db.now(), floor_id))
    log.info("Этаж %s: %s", floor["number"], result["message"])
