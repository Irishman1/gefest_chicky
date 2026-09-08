# -*- coding: utf-8 -*-
"""Нарезка планировок — веб-приложение."""

from __future__ import annotations

import json
import logging
import re
import logging.handlers
import os
import secrets
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from urllib.parse import quote, urlsplit

from . import db, jobs, security

# Логин: латиница ИЛИ кириллица, цифры и . _ - — люди пишут своё имя
# по-русски и по-украински, и отказ по «только латиница» их стопорил.
USERNAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁіІїЇєЄґҐ0-9_.\-]{3,32}$")
from .cutter import (MAX_FLOOR, apply_edits, extract_page, floor_from_filename,
                     make_thumb, page_floors, safe_part)
from .storage import apartments_dir, ensure, floor_dir, project_dir, thumbs_dir

BASE = Path(__file__).resolve().parent
MAX_PDF_MB = int(os.environ.get("MAX_PDF_MB", "40"))
# Альбом на много этажей — обычная подача, поэтому за раз принимаем пачку
# файлов и пачку листов в каждом. Верх нужен, чтобы одна загрузка не забила
# очередь нарезки на полдня.
MAX_UPLOAD_FILES = int(os.environ.get("MAX_UPLOAD_FILES", "30"))
MAX_UPLOAD_PAGES = int(os.environ.get("MAX_UPLOAD_PAGES", "60"))
AUDIT_KEEP_DAYS = int(os.environ.get("AUDIT_KEEP_DAYS", "180"))

templates = Jinja2Templates(directory=str(BASE / "templates"))


LOGIN_WINDOW = 300          # окно счёта неудачных входов, секунд
LOGIN_TRIES = 10            # столько неудач в окне — и адрес ждёт
_login_fails: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def login_blocked(key: str) -> bool:
    """Скрипту с перебором нужно отвечать быстро и одинаково: scrypt дорогой,
    и перебор бьёт не только по паролю, но и по процессу, который режет этаж."""
    now = time.monotonic()
    with _login_lock:
        tries = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW]
        _login_fails[key] = tries
        return len(tries) >= LOGIN_TRIES


def login_failed(key: str) -> None:
    now = time.monotonic()
    with _login_lock:
        if len(_login_fails) > 10000:                  # чтобы словарь не рос вечно
            _login_fails.clear()
        _login_fails.setdefault(key, []).append(now)


def login_ok(key: str) -> None:
    with _login_lock:
        _login_fails.pop(key, None)


def _fmt_time(ts):
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%d.%m.%Y %H:%M")
    except Exception:                                    # noqa: BLE001
        return ""


templates.env.filters["datetime"] = _fmt_time

# Версия статики: StaticFiles не отдаёт Cache-Control, и браузер держит старый
# CSS после деплоя. Метка времени в ссылке заставляет его забрать новый файл.
try:
    STATIC_V = str(int(max(f.stat().st_mtime for f in (BASE / "static").iterdir() if f.is_file())))
except ValueError:                                       # пустая папка static
    STATIC_V = "0"
templates.env.globals["static_v"] = STATIC_V

@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    db.init()
    security.admin_bootstrap()
    housekeeping()
    jobs.requeue_pending()
    log.info("Приложение запущено, данные в %s", db.DATA_DIR)
    yield


def housekeeping() -> None:
    """Просроченные сессии, старый аудит и обрывки загрузок копятся вечно."""
    gone = db.execute_count("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
    old = db.execute_count("DELETE FROM audit WHERE ts < ?",
                           (db.now() - AUDIT_KEEP_DAYS * 86400,))
    stale = 0
    for tmp in db.DATA_DIR.glob("projects/*/incoming/*.pdf"):
        # Файл из incoming живёт секунды: его удаляет сама загрузка. Всё, что
        # осталось лежать, — след упавшего запроса.
        try:
            if db.now() - int(tmp.stat().st_mtime) > 3600:
                tmp.unlink()
                stale += 1
        except OSError:
            pass
    if gone or old or stale:
        log.info("Уборка: сессий %s, записей аудита %s, обрывков загрузки %s",
                 gone, old, stale)


app = FastAPI(title="Нарезка планировок", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

log = logging.getLogger("app")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """
    Любое изменение данных должно приходить с нашей же страницы.

    Токен в каждой форме тут не нужен: заголовок Origin браузер ставит сам и
    подделать его со стороннего сайта нельзя. Где Origin не приходит (форма,
    отправленная тем же сайтом, в части браузеров), сверяем Referer —
    Referrer-Policy ниже гарантирует, что он будет.
    """
    if request.method not in SAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin or urlsplit(origin).netloc != request.url.netloc:
            log.warning("Отклонён %s %s: origin=%r", request.method,
                        request.url.path, origin)
            return templates.TemplateResponse(
                request, "error.html",
                {"user": None, "code": 403,
                 "detail": "Запрос пришёл не с этой страницы. "
                           "Откройте сайт заново и повторите."},
                status_code=403)
    response = await call_next(request)
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


def setup_logging() -> None:
    logs = ensure(db.DATA_DIR / "logs")
    handler = logging.handlers.RotatingFileHandler(
        logs / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler, console]




# --------------------------------------------------------------------- вход
def current_user(request: Request):
    return security.user_by_session(request.cookies.get(security.SESSION_COOKIE, ""))


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Нужно войти")
    return user


def require_admin(request: Request):
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


def page(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("user", current_user(request))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


@app.exception_handler(Exception)
def unhandled_error(request: Request, exc: Exception):
    """Любая необработанная ошибка: полный traceback в лог, пользователю — обычная страница."""
    log.error("Необработанная ошибка на %s %s", request.method, request.url.path,
              exc_info=exc)
    try:
        db.log_action(current_user(request), "error.unhandled",
                      f"{request.method} {request.url.path}: {exc}")
    except Exception:                                    # noqa: BLE001
        pass  # логирование не должно само уронить обработку ошибки
    return templates.TemplateResponse(
        request, "error.html",
        {"user": current_user(request), "code": 500,
         "detail": "Что-то пошло не так. Мы уже записали это в журнал."},
        status_code=500)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "error.html",
        {"user": current_user(request), "code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/projects" if current_user(request) else "/login",
                            status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/projects", status_code=303)
    return page(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    ip = request.client.host if request.client else ""
    if login_blocked(ip) or login_blocked(username):
        db.log_action(None, "login.blocked", username, ip)
        return page(request, "login.html",
                    error="Слишком много попыток. Подождите пять минут.",
                    username=username, status_code=429)
    row = db.one("SELECT * FROM users WHERE username = ?", (username,))
    if not row or not security.check_password(password, row["password_hash"]):
        login_failed(ip)
        login_failed(username)
        db.log_action(None, "login.fail", username, ip)
        return page(request, "login.html", error="Неверный логин или пароль", username=username)
    if not row["is_active"]:
        return page(request, "login.html", error="Учётная запись отключена", username=username)

    login_ok(ip)
    login_ok(username)
    token = security.create_session(row["id"])
    db.log_action(row, "login", "", ip)
    resp = RedirectResponse("/projects", status_code=303)
    resp.set_cookie(security.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=security.SESSION_DAYS * 86400,
                    secure=os.environ.get("COOKIE_SECURE", "0") == "1")
    return resp


@app.get("/logout")
@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(security.SESSION_COOKIE, "")
    user = security.user_by_session(token)
    security.drop_session(token)
    db.log_action(user, "logout")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(security.SESSION_COOKIE)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request, code: str = ""):
    return page(request, "register.html", code=code)


@app.post("/register", response_class=HTMLResponse)
def register(request: Request, code: str = Form(...), username: str = Form(...),
             password: str = Form(...), password2: str = Form(...)):
    username = username.strip().lower()
    invite = db.one("SELECT * FROM invites WHERE code = ? AND used_by IS NULL", (code.strip(),))
    if not invite:
        return page(request, "register.html", code=code, username=username,
                    error="Код приглашения неверный или уже использован")
    if invite["username"] and invite["username"].strip().lower() != username:
        return page(request, "register.html", code=code, username=username,
                    error=f"Это приглашение выписано на логин «{invite['username']}» — "
                          f"введите именно его")
    if not USERNAME_RE.match(username):
        return page(request, "register.html", code=code, username=username,
                    error="Логин: 3–32 символа — буквы (латиница или кириллица), "
                          "цифры, точка, дефис, подчёркивание")
    if len(password) < 8:
        return page(request, "register.html", code=code, username=username,
                    error="Пароль должен быть не короче 8 символов")
    if password != password2:
        return page(request, "register.html", code=code, username=username,
                    error="Пароли не совпадают")
    if db.one("SELECT id FROM users WHERE username = ?", (username,)):
        return page(request, "register.html", code=code, username=username,
                    error="Такой логин уже занят")

    uid = db.execute(
        "INSERT INTO users (username, password_hash, is_admin, is_active, created_at) "
        "VALUES (?,?,0,1,?)", (username, security.hash_password(password), db.now()))
    db.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?",
               (uid, db.now(), invite["code"]))
    token = security.create_session(uid)
    db.log_action({"id": uid, "username": username}, "register")
    resp = RedirectResponse("/projects", status_code=303)
    resp.set_cookie(security.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=security.SESSION_DAYS * 86400,
                    secure=os.environ.get("COOKIE_SECURE", "0") == "1")
    return resp


# ------------------------------------------------------------------ проекты
def get_project(project_id: int, user):
    row = db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not row:
        raise HTTPException(404, "Проект не найден")
    if row["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Это чужой проект")
    return row


@app.get("/projects", response_class=HTMLResponse)
def projects(request: Request, user=Depends(require_user)):
    rows = db.query(
        "SELECT p.*, "
        "(SELECT COUNT(*) FROM floors f WHERE f.project_id = p.id AND f.status='done') "
        "AS ready, "
        "(SELECT COUNT(*) FROM apartments a JOIN floors f ON f.id = a.floor_id "
        " WHERE f.project_id = p.id) AS flats "
        "FROM projects p WHERE p.user_id = ? ORDER BY p.created_at DESC", (user["id"],))
    return page(request, "projects.html", projects=rows)


@app.post("/projects")
def create_project(request: Request, name: str = Form(...), floors: int = Form(...),
                   kind: str = Form(...), user=Depends(require_user)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Укажите название объекта")
    # Тип задаётся явно: жильё и офисы режутся по-разному, и определять это
    # по чертежу — гадание, из-за которого офисы резались неверно.
    if kind not in ("flats", "offices"):
        raise HTTPException(400, "Выберите тип объекта: квартиры или офисы")
    floors = max(1, min(int(floors), MAX_FLOOR))
    pid = db.execute("INSERT INTO projects (user_id, name, floors, kind, created_at) "
                     "VALUES (?,?,?,?,?)", (user["id"], name, floors, kind, db.now()))
    for n in range(1, floors + 1):
        db.execute("INSERT INTO floors (project_id, number, status, updated_at) "
                   "VALUES (?,?,'empty',?)", (pid, n, db.now()))
    db.log_action(user, "project.create",
                  f"{name} ({floors} эт., {'офисы' if kind == 'offices' else 'квартиры'})")
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project(project_id: int, request: Request, user=Depends(require_user)):
    row = get_project(project_id, user)
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    import shutil
    shutil.rmtree(project_dir(project_id), ignore_errors=True)
    db.log_action(user, "project.delete", row["name"])
    return RedirectResponse("/projects", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(project_id: int, request: Request, floor: int = 0, note: str = "",
                 user=Depends(require_user)):
    proj = get_project(project_id, user)
    floors = db.query("SELECT * FROM floors WHERE project_id = ? ORDER BY number",
                      (project_id,))
    if not floors:
        raise HTTPException(404, "У проекта нет этажей")
    current = next((f for f in floors if f["number"] == floor), floors[0])
    apts = db.query("SELECT * FROM apartments WHERE floor_id = ? ORDER BY idx",
                    (current["id"],))
    # рамка нужна на клиенте, чтобы приблизиться к вырезке при правке
    flats = [{"idx": a["idx"], "label": a["label"], "number": a["number"],
              "filename": a["filename"],
              "box": [a["x0"], a["y0"], a["x1"], a["y1"]]} for a in apts]
    return page(request, "project.html", project=proj, floors=floors,
                floor=current, apartments=apts, flats_json=flats,
                note=note[:400])


def _ensure_floor(project_id: int, number: int):
    """Этаж проекта; альбом может накрыть этажи, которых в проекте ещё нет."""
    row = db.one("SELECT * FROM floors WHERE project_id=? AND number=?",
                 (project_id, number))
    if row:
        return row
    for n in range(1, number + 1):
        db.execute("INSERT OR IGNORE INTO floors (project_id, number, status, "
                   "updated_at) VALUES (?,?,'empty',?)", (project_id, n, db.now()))
    db.execute("UPDATE projects SET floors=? WHERE id=? AND floors < ?",
               (number, project_id, number))
    return db.one("SELECT * FROM floors WHERE project_id=? AND number=?",
                  (project_id, number))


def _ranges(numbers) -> str:
    """[14,15,16,19] -> \u00ab14\u201316, 19\u00bb \u2014 чтобы не перечислять двадцать этажей."""
    out, start, prev = [], None, None
    for n in sorted(set(numbers)):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(str(start) if start == prev else f"{start}\u2013{prev}")
            start = prev = n
    if start is not None:
        out.append(str(start) if start == prev else f"{start}\u2013{prev}")
    return ", ".join(out)


@app.post("/projects/{project_id}/floors/{number}/upload")
async def upload_pdf(project_id: int, number: int, request: Request,
                     pdf: list[UploadFile] = File(...), user=Depends(require_user)):
    """
    Принимает один PDF или сразу несколько, и любой из них может быть альбомом
    на много этажей.

    Один лист — режем на выбранный этаж, как и раньше: человек уже указал его
    в интерфейсе, спорить с ним по чертежу незачем. Листов больше одного —
    этаж каждого определяем по самому чертежу (подписи квартир, потом надпись
    в штампе), а те, где не вышло, раскладываем подряд.
    """
    proj = get_project(project_id, user)
    if not db.one("SELECT 1 FROM floors WHERE project_id=? AND number=?",
                  (project_id, number)):
        raise HTTPException(404, "Этаж не найден")

    files = [f for f in pdf if (f.filename or "").strip()]
    if not files:
        raise HTTPException(400, "Выберите файл PDF")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"За раз не больше {MAX_UPLOAD_FILES} файлов")

    incoming = ensure(project_dir(project_id) / "incoming")
    saved = []                              # (файл во временной папке, имя)
    try:
        for up in files:
            name = up.filename or ""
            if not name.lower().endswith(".pdf"):
                raise HTTPException(400, f"«{name}» — нужен файл PDF")
            data = await up.read()
            if len(data) > MAX_PDF_MB * 1024 * 1024:
                raise HTTPException(400, f"«{name}» больше {MAX_PDF_MB} МБ")
            if not data.startswith(b"%PDF"):
                raise HTTPException(400, f"«{name}» не похож на PDF")
            tmp = incoming / f"{secrets.token_hex(8)}.pdf"
            tmp.write_bytes(data)
            saved.append((tmp, name))

        sheets = []                     # (файл, имя, номер листа, найденный этаж)
        for tmp, name in saved:
            try:
                found = page_floors(tmp)
            except Exception:                            # noqa: BLE001
                log.warning("PDF не читается: %s", name, exc_info=True)
                raise HTTPException(400, f"«{name}» не читается как PDF")
            if not found:
                raise HTTPException(400, f"В «{name}» нет страниц")
            # У однолистового файла этаж часто написан только в имени.
            by_name = floor_from_filename(name) if len(found) == 1 else None
            for i, fl in enumerate(found):
                sheets.append((tmp, name, i, fl if fl is not None else by_name))
        if len(sheets) > MAX_UPLOAD_PAGES:
            raise HTTPException(400, f"За раз не больше {MAX_UPLOAD_PAGES} листов")

        plan, skipped = [], []
        if len(sheets) == 1:
            tmp, name, i, _fl = sheets[0]
            plan.append((number, tmp, name, i))
        else:
            nxt, taken = number, set()
            for tmp, name, i, fl in sheets:
                target = max(1, min(fl or nxt, MAX_FLOOR))
                if target in taken:
                    skipped.append(f"{name}, лист {i + 1}: этаж {target} "
                                   f"в этой загрузке уже занят")
                    continue
                taken.add(target)
                nxt = target + 1
                plan.append((target, tmp, name, i))
        if not plan:
            raise HTTPException(400, "Ни один лист не удалось разложить по этажам")

        single = len(plan) == 1
        for target, tmp, name, i in plan:
            floor = _ensure_floor(project_id, target)
            d = ensure(floor_dir(project_id, floor["id"]))
            extract_page(tmp, i, d / "source.pdf")
            db.execute("UPDATE floors SET pdf_name=?, status='queued', "
                       "message='В очереди', log=NULL, updated_at=? WHERE id=?",
                       (name if single else f"{name} (лист {i + 1})",
                        db.now(), floor["id"]))
            db.execute("DELETE FROM apartments WHERE floor_id=?", (floor["id"],))
            # Ручные правки сделаны по прежнему чертежу этого этажа — на новый
            # их накладывать нельзя, лягут мимо.
            db.execute("DELETE FROM floor_edits WHERE floor_id=?", (floor["id"],))
            jobs.enqueue(floor["id"])

        done = [t for t, *_rest in plan]
        note = "" if single else f"Загружено листов: {len(done)} → этажи {_ranges(done)}."
        if skipped:
            note = (note + " Пропущено: " + "; ".join(skipped)).strip()
        db.log_action(user, "floor.upload",
                      f"{proj['name']}: листов {len(done)}, эт. {_ranges(done)}")
        url = f"/projects/{project_id}?floor={min(done)}"
        if note:
            url += "&note=" + quote(note)
        return RedirectResponse(url, status_code=303)
    finally:
        for tmp, _name in saved:
            tmp.unlink(missing_ok=True)


@app.post("/projects/{project_id}/floors/{number}/recut")
def recut(project_id: int, number: int, request: Request, user=Depends(require_user)):
    proj = get_project(project_id, user)
    floor = db.one("SELECT * FROM floors WHERE project_id=? AND number=?",
                   (project_id, number))
    if not floor or not (floor_dir(project_id, floor["id"]) / "source.pdf").exists():
        raise HTTPException(400, "Сначала загрузите PDF")
    if floor["status"] in ("queued", "working"):
        raise HTTPException(400, "Этаж уже в очереди")
    jobs.enqueue(floor["id"])
    db.log_action(user, "floor.recut", f"{proj['name']} эт.{number}")
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


# ------------------------------------------------------- ручная правка нарезки
def _floor_for_edit(project_id: int, number: int, user):
    proj = get_project(project_id, user)
    floor = db.one("SELECT * FROM floors WHERE project_id=? AND number=?",
                   (project_id, number))
    if not floor:
        raise HTTPException(404, "Этаж не найден")
    d = floor_dir(project_id, floor["id"])
    if not (d / "source.pdf").exists():
        raise HTTPException(400, "Сначала загрузите PDF")
    # Правка меняет те же файлы и тот же hitmap, которые прямо сейчас
    # перезаписывает фоновая нарезка. Без этого запрета правка молча пропадала
    # или портила карту попаданий.
    if floor["status"] in ("queued", "working"):
        raise HTTPException(400, "Этаж сейчас режется — дождитесь окончания")
    return proj, floor, d


def _records_of(floor_id: int) -> list:
    return [{"idx": r["idx"], "label": r["label"], "number": r["number"],
             "filename": r["filename"], "box": (r["x0"], r["y0"], r["x1"], r["y1"])}
            for r in db.query("SELECT * FROM apartments WHERE floor_id=? ORDER BY idx",
                              (floor_id,))]


def _save_records(floor_id: int, records: list) -> None:
    db.execute("DELETE FROM apartments WHERE floor_id=?", (floor_id,))
    for rec in records:
        x0, y0, x1, y1 = rec["box"]
        db.execute("INSERT INTO apartments (floor_id, idx, label, number, filename, "
                   "x0, y0, x1, y1) VALUES (?,?,?,?,?,?,?,?,?)",
                   (floor_id, rec["idx"], rec["label"], rec["number"],
                    rec["filename"], x0, y0, x1, y1))


def _record_edit(floor_id: int, edit: dict) -> None:
    """
    Кладёт правку в журнал, сворачивая то, что она перекрыла.

    Журнал переигрывается целиком при каждой автонарезке, поэтому десять раз
    перерисованный контур не должен превращаться в двадцать шагов замены.
    Свёртка меняет только длину журнала, но не итог его проигрывания.
    """
    action, target, number = edit["action"], edit["target"], edit["number"]
    if action == "add":
        # Новый контур для того же номера заменяет прежний ручной.
        db.execute("DELETE FROM floor_edits WHERE floor_id=? AND action='add' "
                   "AND number=?", (floor_id, number))
    elif action == "delete":
        dropped = db.execute_count(
            "DELETE FROM floor_edits WHERE floor_id=? AND action='add' AND number=?",
            (floor_id, target))
        if dropped:
            # Удаляем то, что сами же и дорисовали, — в журнале не остаётся
            # ни добавления, ни удаления.
            return
    elif action == "rename":
        # A->B, потом B->C: в журнале должно остаться A->C.
        for act in ("add", "rename"):
            if db.execute_count(
                    "UPDATE floor_edits SET number=? WHERE floor_id=? AND action=? "
                    "AND number=?", (number, floor_id, act, target)):
                return
    db.execute("INSERT INTO floor_edits (floor_id, action, target, number, polygon, "
               "created_at) VALUES (?,?,?,?,?,?)",
               (floor_id, action, target, number, edit["polygon"], db.now()))


def _apply_one(proj, floor, d, edit: dict, user, what: str):
    """Пишет правку в журнал правок и сразу накладывает её на файлы этажа."""
    _record_edit(floor["id"], edit)
    records = _records_of(floor["id"])
    records = apply_edits(d / "source.pdf", d, proj["name"], floor["number"],
                          records, [edit])
    _save_records(floor["id"], records)
    db.execute("UPDATE floors SET message=?, updated_at=? WHERE id=?",
               (f"Готово: {len(records)}", db.now(), floor["id"]))
    db.log_action(user, f"floor.edit.{edit['action']}",
                  f"{proj['name']} эт.{floor['number']}: {what}")


@app.post("/projects/{project_id}/floors/{number}/edits/add")
def edit_add(project_id: int, number: int, request: Request,
             flat: str = Form(...), polygon: str = Form(...),
             user=Depends(require_user)):
    proj, floor, d = _floor_for_edit(project_id, number, user)
    flat = flat.strip()
    if not flat:
        raise HTTPException(400, "Укажите номер")
    try:
        points = json.loads(polygon)
    except ValueError:
        raise HTTPException(400, "Контур не разобран")
    if not isinstance(points, list) or len(points) < 3:
        raise HTTPException(400, "В контуре нужно хотя бы три точки")
    if db.one("SELECT 1 FROM apartments WHERE floor_id=? AND number=?",
              (floor["id"], flat)):
        raise HTTPException(400, f"Номер {flat} на этом этаже уже есть")
    _apply_one(proj, floor, d,
               {"action": "add", "target": "", "number": flat,
                "polygon": json.dumps(points)}, user, flat)
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


@app.post("/projects/{project_id}/floors/{number}/edits/replace")
def edit_replace(project_id: int, number: int, request: Request,
                 target: str = Form(...), flat: str = Form(...),
                 polygon: str = Form(...), user=Depends(require_user)):
    """Перерисовать контур: старый убираем и тут же кладём новый."""
    proj, floor, d = _floor_for_edit(project_id, number, user)
    flat = flat.strip()
    if not flat:
        raise HTTPException(400, "Укажите номер")
    try:
        points = json.loads(polygon)
    except ValueError:
        raise HTTPException(400, "Контур не разобран")
    if not isinstance(points, list) or len(points) < 3:
        raise HTTPException(400, "В контуре нужно хотя бы три точки")
    if flat != target and db.one("SELECT 1 FROM apartments WHERE floor_id=? AND number=?",
                                 (floor["id"], flat)):
        raise HTTPException(400, f"Номер {flat} на этом этаже уже есть")
    _apply_one(proj, floor, d,
               {"action": "delete", "target": target, "number": "", "polygon": ""},
               user, target)
    _apply_one(proj, floor, d,
               {"action": "add", "target": "", "number": flat,
                "polygon": json.dumps(points)}, user, flat)
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


@app.post("/projects/{project_id}/floors/{number}/edits/rename")
def edit_rename(project_id: int, number: int, request: Request,
                target: str = Form(...), flat: str = Form(...),
                user=Depends(require_user)):
    proj, floor, d = _floor_for_edit(project_id, number, user)
    flat = flat.strip()
    if not flat:
        raise HTTPException(400, "Укажите номер")
    if flat != target and db.one("SELECT 1 FROM apartments WHERE floor_id=? AND number=?",
                                 (floor["id"], flat)):
        raise HTTPException(400, f"Номер {flat} на этом этаже уже есть")
    _apply_one(proj, floor, d,
               {"action": "rename", "target": target, "number": flat, "polygon": ""},
               user, f"{target} -> {flat}")
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


@app.post("/projects/{project_id}/floors/{number}/edits/delete")
def edit_delete(project_id: int, number: int, request: Request,
                target: str = Form(...), user=Depends(require_user)):
    proj, floor, d = _floor_for_edit(project_id, number, user)
    _apply_one(proj, floor, d,
               {"action": "delete", "target": target, "number": "", "polygon": ""},
               user, target)
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


@app.post("/projects/{project_id}/floors/{number}/edits/reset")
def edit_reset(project_id: int, number: int, request: Request,
               user=Depends(require_user)):
    """Сбрасывает ручные правки и пересчитывает этаж заново."""
    proj, floor, _d = _floor_for_edit(project_id, number, user)
    db.execute("DELETE FROM floor_edits WHERE floor_id=?", (floor["id"],))
    jobs.enqueue(floor["id"])
    db.log_action(user, "floor.edit.reset", f"{proj['name']} эт.{number}")
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


# --------------------------------------------------------------------- API
def get_floor(floor_id: int, user):
    row = db.one("SELECT f.*, p.user_id, p.name AS project_name, p.id AS pid "
                 "FROM floors f JOIN projects p ON p.id=f.project_id WHERE f.id=?",
                 (floor_id,))
    if not row:
        raise HTTPException(404, "Этаж не найден")
    if row["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "Это чужой проект")
    return row


@app.get("/api/floors/{floor_id}")
def floor_status(floor_id: int, request: Request, user=Depends(require_user)):
    floor = get_floor(floor_id, user)
    apts = db.query("SELECT idx, label, number, filename, x0, y0, x1, y1 "
                    "FROM apartments WHERE floor_id=? ORDER BY idx", (floor_id,))
    return {
        "id": floor_id,
        "status": floor["status"],
        "message": floor["message"] or "",
        "apartments": [dict(a) for a in apts],
    }


@app.get("/files/floors/{floor_id}/{name}")
def floor_file(floor_id: int, name: str, request: Request, user=Depends(require_user)):
    floor = get_floor(floor_id, user)
    if name not in {"preview.png", "hitmap.png", "source.pdf"}:
        raise HTTPException(404, "Нет такого файла")
    path = floor_dir(floor["pid"], floor_id) / name
    if not path.exists():
        raise HTTPException(404, "Файл ещё не готов")
    return FileResponse(path)


@app.get("/files/floors/{floor_id}/apartments/{name}")
def apartment_file(floor_id: int, name: str, request: Request, download: int = 0,
                   user=Depends(require_user)):
    floor = get_floor(floor_id, user)
    row = db.one("SELECT * FROM apartments WHERE floor_id=? AND filename=?",
                 (floor_id, name))
    if not row:
        raise HTTPException(404, "Квартира не найдена")
    path = apartments_dir(floor["pid"], floor_id) / row["filename"]
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    if download:
        db.log_action(user, "download.apartment", f"{floor['project_name']} {name}")
        return FileResponse(path, filename=row["filename"],
                            media_type="application/octet-stream")
    return FileResponse(path)


@app.get("/files/floors/{floor_id}/thumbs/{name}")
def apartment_thumb(floor_id: int, name: str, request: Request,
                    user=Depends(require_user)):
    """Миниатюра вырезки для списка этажа; если её нет — отдаём полный файл."""
    floor = get_floor(floor_id, user)
    row = db.one("SELECT * FROM apartments WHERE floor_id=? AND filename=?",
                 (floor_id, name))
    if not row:
        raise HTTPException(404, "Квартира не найдена")
    small = thumbs_dir(floor["pid"], floor_id) / row["filename"]
    if small.exists():
        return FileResponse(small)
    full = apartments_dir(floor["pid"], floor_id) / row["filename"]
    if not full.exists():
        raise HTTPException(404, "Файл не найден")
    # Этажи, нарезанные до появления миниатюр, своей папки thumbs не имеют.
    # Делаем миниатюру на месте и кладём рядом — иначе список этажа тянул бы
    # десятки полноразмерных PNG.
    try:
        make_thumb(full, small.parent)
        return FileResponse(small)
    except Exception:                                # noqa: BLE001
        log.warning("Не удалось сделать миниатюру для %s", full, exc_info=True)
        return FileResponse(full)


def content_disposition(filename: str) -> str:
    """Кириллица в имени файла ломает старый заголовок — отдаём в кодировке RFC 5987."""
    ascii_fallback = filename.encode("ascii", "ignore").decode() or "file.zip"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def zip_to_file(files: list[tuple[Path, str]]) -> Path:
    """
    Складывает архив во временный файл и отдаёт путь.

    В памяти его собирать нельзя: этаж — это два десятка PNG по 300 dpi, а
    объект — все его этажи разом, и на маленьком контейнере такой архив
    встречается с нарезкой, которая сама берёт под себя сотни мегабайт.
    PNG и JPG уже сжаты, поэтому кладём без сжатия: ZIP_STORED быстрее и не
    отбирает процессор у очереди нарезки.
    """
    tmp_dir = ensure(db.DATA_DIR / "tmp")
    fd, name = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for src, arc in files:
            if src.exists():
                z.write(src, arc)
    return path


def zip_response(files: list[tuple[Path, str]], name: str) -> FileResponse:
    path = zip_to_file(files)
    return FileResponse(
        path, media_type="application/zip",
        headers={"Content-Disposition": content_disposition(name)},
        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get("/download/floor/{floor_id}")
def download_floor(floor_id: int, request: Request, user=Depends(require_user)):
    floor = get_floor(floor_id, user)
    rows = db.query("SELECT filename FROM apartments WHERE floor_id=? ORDER BY idx",
                    (floor_id,))
    if not rows:
        raise HTTPException(404, "Нечего скачивать")
    d = apartments_dir(floor["pid"], floor_id)
    name = f"{floor['project_name']}_{floor['number']}.zip"
    db.log_action(user, "download.floor", name)
    return zip_response([(d / r["filename"], r["filename"]) for r in rows], name)


@app.get("/download/project/{project_id}")
def download_project(project_id: int, request: Request, user=Depends(require_user)):
    proj = get_project(project_id, user)
    rows = db.query(
        "SELECT a.filename, f.id AS fid, f.number FROM apartments a "
        "JOIN floors f ON f.id=a.floor_id WHERE f.project_id=? ORDER BY f.number, a.idx",
        (project_id,))
    if not rows:
        raise HTTPException(404, "В проекте ещё нет нарезанных квартир")
    files = [(apartments_dir(project_id, r["fid"]) / r["filename"],
              f"{r['number']}/{r['filename']}") for r in rows]
    name = f"{proj['name']}.zip"
    db.log_action(user, "download.project", name)
    return zip_response(files, name)


# ------------------------------------------------------------------ админка
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, user=Depends(require_admin)):
    users = db.query("SELECT * FROM users ORDER BY created_at")
    invites = db.query("SELECT * FROM invites ORDER BY created_at DESC LIMIT 50")
    audit = db.query("SELECT * FROM audit ORDER BY ts DESC LIMIT 100")
    return page(request, "admin.html", users=users, invites=invites, audit=audit)


@app.post("/admin/invite")
def make_invite(request: Request, username: str = Form(""), user=Depends(require_admin)):
    code = security.new_invite_code()
    db.execute("INSERT INTO invites (code, username, created_by, created_at) VALUES (?,?,?,?)",
               (code, username.strip().lower() or None, user["id"], db.now()))
    db.log_action(user, "invite.create", username)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
def toggle_user(user_id: int, request: Request, user=Depends(require_admin)):
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(404, "Пользователь не найден")
    if row["id"] == user["id"]:
        raise HTTPException(400, "Нельзя отключить самого себя")
    db.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (user_id,))
    db.log_action(user, "user.toggle", row["username"])
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/backup")
def admin_backup(request: Request, user=Depends(require_admin)):
    import datetime

    from .backup_util import make_backup_file

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    name = f"backup-{stamp}.zip"
    tmp_dir = ensure(db.DATA_DIR / "tmp")
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    os.close(fd)
    path = make_backup_file(Path(tmp))
    db.log_action(user, "backup.download", name)
    return FileResponse(path, media_type="application/zip",
                        headers={"Content-Disposition": content_disposition(name)},
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.post("/admin/restore")
async def admin_restore(request: Request, archive: UploadFile = File(...),
                        user=Depends(require_admin)):
    from .backup_util import restore_backup_file

    # Архив целиком в памяти держать незачем — пишем на диск кусками.
    tmp_dir = ensure(db.DATA_DIR / "tmp")
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
    os.close(fd)
    path = Path(tmp)
    try:
        with path.open("wb") as fh:
            while chunk := await archive.read(1 << 20):
                fh.write(chunk)
        restore_backup_file(path)
    finally:
        path.unlink(missing_ok=True)
    db.log_action(user, "backup.restore", archive.filename or "")
    return RedirectResponse("/admin", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}
