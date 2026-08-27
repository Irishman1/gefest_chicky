# -*- coding: utf-8 -*-
"""Нарезка планировок — веб-приложение."""

from __future__ import annotations

import io
import json
import logging
import re
import logging.handlers
import os
import zipfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from urllib.parse import quote

from . import db, jobs, security

# Логин: латиница ИЛИ кириллица, цифры и . _ - — люди пишут своё имя
# по-русски и по-украински, и отказ по «только латиница» их стопорил.
USERNAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁіІїЇєЄґҐ0-9_.\-]{3,32}$")
from .cutter import apply_edits, make_thumb, safe_part
from .storage import apartments_dir, ensure, floor_dir, project_dir, thumbs_dir

BASE = Path(__file__).resolve().parent
MAX_PDF_MB = int(os.environ.get("MAX_PDF_MB", "40"))

templates = Jinja2Templates(directory=str(BASE / "templates"))


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

app = FastAPI(title="Нарезка планировок")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

log = logging.getLogger("app")


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


@app.on_event("startup")
def startup() -> None:
    setup_logging()
    db.init()
    security.admin_bootstrap()
    jobs.requeue_pending()
    log.info("Приложение запущено, данные в %s", db.DATA_DIR)


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


def page(request: Request, name: str, **ctx):
    ctx.setdefault("user", current_user(request))
    return templates.TemplateResponse(request, name, ctx)


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
    row = db.one("SELECT * FROM users WHERE username = ?", (username,))
    if not row or not security.check_password(password, row["password_hash"]):
        db.log_action(None, "login.fail", username, request.client.host if request.client else "")
        return page(request, "login.html", error="Неверный логин или пароль", username=username)
    if not row["is_active"]:
        return page(request, "login.html", error="Учётная запись отключена", username=username)

    token = security.create_session(row["id"])
    db.log_action(row, "login", "", request.client.host if request.client else "")
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
    floors = max(1, min(int(floors), 200))
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
def project_page(project_id: int, request: Request, floor: int = 0,
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
                floor=current, apartments=apts, flats_json=flats)


@app.post("/projects/{project_id}/floors/{number}/upload")
async def upload_pdf(project_id: int, number: int, request: Request,
                     pdf: UploadFile = File(...), user=Depends(require_user)):
    proj = get_project(project_id, user)
    floor = db.one("SELECT * FROM floors WHERE project_id=? AND number=?",
                   (project_id, number))
    if not floor:
        raise HTTPException(404, "Этаж не найден")
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Нужен файл PDF")

    data = await pdf.read()
    if len(data) > MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(400, f"Файл больше {MAX_PDF_MB} МБ")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Это не похоже на PDF")

    d = ensure(floor_dir(project_id, floor["id"]))
    (d / "source.pdf").write_bytes(data)
    db.execute("UPDATE floors SET pdf_name=?, status='queued', message='В очереди', "
               "log=NULL, updated_at=? WHERE id=?",
               (pdf.filename, db.now(), floor["id"]))
    db.execute("DELETE FROM apartments WHERE floor_id=?", (floor["id"],))
    jobs.enqueue(floor["id"])
    db.log_action(user, "floor.upload", f"{proj['name']} эт.{number}: {pdf.filename}")
    return RedirectResponse(f"/projects/{project_id}?floor={number}", status_code=303)


@app.post("/projects/{project_id}/floors/{number}/recut")
def recut(project_id: int, number: int, request: Request, user=Depends(require_user)):
    proj = get_project(project_id, user)
    floor = db.one("SELECT * FROM floors WHERE project_id=? AND number=?",
                   (project_id, number))
    if not floor or not (floor_dir(project_id, floor["id"]) / "source.pdf").exists():
        raise HTTPException(400, "Сначала загрузите PDF")
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


def _apply_one(proj, floor, d, edit: dict, user, what: str):
    """Пишет правку в журнал правок и сразу накладывает её на файлы этажа."""
    db.execute("INSERT INTO floor_edits (floor_id, action, target, number, polygon, "
               "created_at) VALUES (?,?,?,?,?,?)",
               (floor["id"], edit["action"], edit["target"], edit["number"],
                edit["polygon"], db.now()))
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


def zip_stream(files: list[tuple[Path, str]]):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in files:
            if path.exists():
                z.write(path, name)
    buf.seek(0)
    return buf


@app.get("/download/floor/{floor_id}")
def download_floor(floor_id: int, request: Request, user=Depends(require_user)):
    floor = get_floor(floor_id, user)
    rows = db.query("SELECT filename FROM apartments WHERE floor_id=? ORDER BY idx",
                    (floor_id,))
    if not rows:
        raise HTTPException(404, "Нечего скачивать")
    d = apartments_dir(floor["pid"], floor_id)
    buf = zip_stream([(d / r["filename"], r["filename"]) for r in rows])
    name = f"{floor['project_name']}_{floor['number']}.zip"
    db.log_action(user, "download.floor", name)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": content_disposition(name)})


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
    buf = zip_stream(files)
    name = f"{proj['name']}.zip"
    db.log_action(user, "download.project", name)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": content_disposition(name)})


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

    from .backup_util import make_backup_bytes

    data = make_backup_bytes()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    name = f"backup-{stamp}.zip"
    db.log_action(user, "backup.download", name)
    return StreamingResponse(io.BytesIO(data), media_type="application/zip",
                             headers={"Content-Disposition": content_disposition(name)})


@app.post("/admin/restore")
async def admin_restore(request: Request, archive: UploadFile = File(...),
                        user=Depends(require_admin)):
    from .backup_util import restore_backup_bytes

    data = await archive.read()
    restore_backup_bytes(data)
    db.log_action(user, "backup.restore", archive.filename or "")
    return RedirectResponse("/admin", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}
