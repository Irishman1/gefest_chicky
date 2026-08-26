# -*- coding: utf-8 -*-
"""Пароли и сессии. Без внешних зависимостей: scrypt из стандартной библиотеки."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from . import db

SESSION_COOKIE = "sid"
SESSION_DAYS = 30
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, key_hex = stored.split("$")
        if algo != "scrypt":
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **SCRYPT)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:                                    # noqa: BLE001
        return False


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    ts = db.now()
    db.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, user_id, ts, ts + SESSION_DAYS * 86400),
    )
    return token


def drop_session(token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def user_by_session(token: str):
    if not token:
        return None
    row = db.one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1",
        (token, db.now()),
    )
    return row


def new_invite_code() -> str:
    return secrets.token_urlsafe(9)


def admin_bootstrap() -> None:
    """Создаёт первого администратора из переменных окружения, если пользователей нет."""
    if db.one("SELECT id FROM users LIMIT 1"):
        return
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or not password:
        return
    db.execute(
        "INSERT INTO users (email, password_hash, is_admin, is_active, created_at) "
        "VALUES (?,?,1,1,?)",
        (email, hash_password(password), db.now()),
    )
