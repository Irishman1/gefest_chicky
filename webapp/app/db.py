# -*- coding: utf-8 -*-
"""База данных: SQLite. Один файл — его же и бэкапить."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    username   TEXT,
    created_by INTEGER REFERENCES users(id),
    used_by    INTEGER REFERENCES users(id),
    created_at INTEGER NOT NULL,
    used_at    INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    floors     INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'flats',   -- flats | offices
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS floors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    number     INTEGER NOT NULL,
    pdf_name   TEXT,
    status     TEXT NOT NULL DEFAULT 'empty',   -- empty|queued|working|done|error
    message    TEXT,
    log        TEXT,
    updated_at INTEGER NOT NULL,
    UNIQUE (project_id, number)
);

CREATE TABLE IF NOT EXISTS apartments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    floor_id INTEGER NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    idx      INTEGER NOT NULL,                  -- номер в hit-карте
    label    TEXT NOT NULL,                     -- А-12.2
    number   TEXT NOT NULL,                     -- 2
    filename TEXT NOT NULL,                     -- Еллада_12_2.png
    x0 REAL, y0 REAL, x1 REAL, y1 REAL          -- рамка в координатах превью
);

-- Ручные правки нарезки. Живут отдельно от результата и накладываются
-- поверх него заново после каждой автонарезки — иначе «порезать заново»
-- стирало бы всю ручную работу.
CREATE TABLE IF NOT EXISTS floor_edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    floor_id   INTEGER NOT NULL REFERENCES floors(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,                  -- add | rename | delete
    target     TEXT NOT NULL DEFAULT '',       -- к какому номеру относится
    number     TEXT NOT NULL DEFAULT '',       -- новый номер (add/rename)
    polygon    TEXT NOT NULL DEFAULT '',       -- JSON [[x,y]...] в долях 0..1
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    user_id  INTEGER,
    username TEXT,
    action  TEXT NOT NULL,
    details TEXT,
    ip      TEXT
);

CREATE INDEX IF NOT EXISTS idx_floors_project ON floors(project_id);
CREATE INDEX IF NOT EXISTS idx_apartments_floor ON apartments(floor_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Проекты, созданные до появления выбора типа, считаем жилыми:
        # раньше сайт умел резать только их.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
        if "kind" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL "
                         "DEFAULT 'flats'")
        conn.commit()


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def one(sql: str, args: tuple = ()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def log_action(user, action: str, details: str = "", ip: str = "") -> None:
    execute(
        "INSERT INTO audit (ts, user_id, username, action, details, ip) VALUES (?,?,?,?,?,?)",
        (now(), user["id"] if user else None, user["username"] if user else None,
         action, details, ip),
    )
