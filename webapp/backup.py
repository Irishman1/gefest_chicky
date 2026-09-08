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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Один и тот же архиватор, что и в админке: там уже и сброс WAL в базу,
# и пропуск рабочих каталогов.
from app.backup_util import make_backup_file  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="путь к архиву (по умолчанию backups/…)")
    args = ap.parse_args()

    if args.out:
        out = Path(args.out)
    else:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
        out = Path(__file__).resolve().parent / "backups" / f"backup-{stamp}.zip"

    path = make_backup_file(out)
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"Готово: {path} ({size_mb:.1f} МБ)")


if __name__ == "__main__":
    main()
