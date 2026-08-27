# -*- coding: utf-8 -*-
"""
Обёртка над cut_apartments для сайта.

Режет PDF этажа и складывает:
  source.pdf                — исходник
  preview.png               — план этажа для просмотра
  hitmap.png                — карта попаданий: цвет пикселя = номер квартиры
  apartments/<Объект>_<этаж>_<квартира>.png
"""

from __future__ import annotations

import shutil
import sys
import unicodedata
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cut_apartments as ca  # noqa: E402

PREVIEW_DPI = 110          # план для просмотра в браузере
INVALID = '<>:"/\\|?*'


def safe_part(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text)).strip()
    for ch in INVALID:
        text = text.replace(ch, "_")
    text = "_".join(text.split())
    return text.strip(" .") or "obj"


def file_name(project_name: str, floor: int | str, number: str, ext: str = ".png") -> str:
    """Еллада_12_2.png — объект, этаж, квартира."""
    return f"{safe_part(project_name)}_{safe_part(floor)}_{safe_part(number)}{ext}"


def cut_floor(pdf_path: Path, out_dir: Path, project_name: str, floor_number: int,
              log=None, dpi: int = 300, bg: str = "white",
              kind: str = "flats") -> dict:
    """Режет первую страницу PDF. Возвращает данные для базы."""
    lines: list[str] = []

    def say(msg=""):
        lines.append(str(msg))
        if log:
            log(str(msg))

    out_dir.mkdir(parents=True, exist_ok=True)
    apt_dir = out_dir / "apartments"
    if apt_dir.exists():
        shutil.rmtree(apt_dir)
    apt_dir.mkdir(parents=True, exist_ok=True)

    # Тип объекта задаёт пользователь при создании проекта. Жильё и офисы
    # устроены по-разному: у квартир границу держат стены, у офисов —
    # цвет заливки, и угадывать это по чертежу ненадёжно.
    mode = "zone" if kind == "offices" else "auto"
    what = "офисов" if kind == "offices" else "квартир"
    args = ca.default_args(dpi=dpi, bg=bg, floor=str(floor_number), mode=mode)
    tess = ca.find_tesseract()

    doc = fitz.open(pdf_path)
    try:
        if not len(doc):
            raise ValueError("В PDF нет страниц")
        page = doc[0]

        def getter(d, p=page):
            return ca.page_to_image(p, d)

        apts, masks, info = ca.cut_page(page, getter, args, tess,
                                        page_no=floor_number,
                                        name_floor=str(floor_number), log=say)
        if not apts:
            reason = {
                "no-labels": (f"не найдены подписи ({what}). Для офисов нужны выноски "
                              f"«№N» с полей листа — проверьте, тот ли тип объекта выбран"
                              if kind == "offices" else
                              "не найдены подписи квартир (нужен текстовый слой или скан получше)"),
                "no-zones": "не найдены цветные зоны помещений — это не похоже на офисный план",
                "no-fills": f"не найдены цветные заливки ({what})",
                "no-apartments": f"{what} не распознаны",
            }.get(info.get("error"), f"{what} не распознаны")
            return {"ok": False, "message": reason, "log": "\n".join(lines),
                    "apartments": []}

        base_img = ca.page_to_image(page, dpi)
        preview = ca.page_to_image(page, PREVIEW_DPI)
        preview.save(out_dir / "preview.png", optimize=True)

        pad_px = max(int(round(args.padding * dpi / 72.0)), 0)
        ext = ca.ext_for(bg)
        k = PREVIEW_DPI / info["mask_dpi"]
        hit = np.zeros((preview.height, preview.width), np.uint8)

        records, used = [], set()
        for i, (apt, mask) in enumerate(zip(apts, masks), start=1):
            name = file_name(project_name, floor_number, apt.number, ext)
            if name in used:
                name = file_name(project_name, floor_number, f"{apt.number}_{i}", ext)
            used.add(name)
            saved = ca.save_apartment(base_img, mask, apt_dir / name,
                                      pad_px, bg, args.quality)
            if not saved:
                say(f"      {apt.label}: пустая маска, пропущено")
                continue

            small = np.asarray(Image.fromarray(mask).resize(
                (preview.width, preview.height), Image.NEAREST))
            hit[small & (hit == 0)] = i
            ys, xs = np.nonzero(small)
            box = ((float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
                   if xs.size else (0.0, 0.0, 0.0, 0.0))
            records.append({"idx": i, "label": apt.label, "number": apt.number,
                            "filename": name, "box": box})
            say(f"      {apt.label} -> {name}")

        Image.fromarray(hit, mode="L").save(out_dir / "hitmap.png", optimize=True)

        if info["missing"]:
            say("      без заливки (не вырезаны): " + ", ".join(info["missing"]))
        if info["orphans"]:
            say(f"      осталось непривязанных заливок: {info['orphans']} "
                "(обычно это легенда)")

        return {"ok": True, "apartments": records, "log": "\n".join(lines),
                "mode": info["mode"],
                "message": f"Готово: {what} {len(records)}"}
    finally:
        doc.close()
