# -*- coding: utf-8 -*-
"""
Бэкап: упаковывает все данные (базу и файлы проектов) в один zip.

Запуск:
  python backup.py                  -> backups/backup-2026-08-26_1200.zip
  python backup.py --out mydump.zip -> в указанный файл

На Railway: railway run python webapp/backup.py
(команда выполняется в контейнере, где смонтирован volume с данными;
готовый файл можно потом скачать через `railway run cat ... > local.zip`
или через админ-эндпоинт /admin/backup, см. app/main.py).
"""

from __future__ import annotations

import argparse
import datetime
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.db import DATA_DIR  # noqa: E402


def make_backup(out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in DATA_DIR.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(DATA_DIR))
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="путь к архиву (по умолчанию backups/…)")
    args = ap.parse_args()

    if args.out:
        out = Path(args.out)
    else:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        out = Path(__file__).resolve().parent / "backups" / f"backup-{stamp}.zip"

    path = make_backup(out)
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"Готово: {path} ({size_mb:.1f} МБ)")


if __name__ == "__main__":
    main()
