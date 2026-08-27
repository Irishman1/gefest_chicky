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

import json
import shutil
import sys
import unicodedata
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw

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


THUMB_MAX = 360           # длинная сторона миниатюры вырезки


def make_thumb(src: Path, dst_dir: Path) -> None:
    """Кладёт рядом уменьшенную копию — список этажа грузит её, а не полный PNG."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.copy()
    im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
    im.save(dst_dir / src.name, optimize=True)


def _polygon_mask(points, width: int, height: int) -> np.ndarray:
    """Контур в долях 0..1 -> булева маска нужного размера."""
    pts = [(float(x) * width, float(y) * height) for x, y in points]
    img = Image.new("L", (width, height), 0)
    ImageDraw.Draw(img).polygon(pts, fill=255)
    return np.asarray(img) > 0


def apply_edits(pdf_path: Path, out_dir: Path, project_name: str, floor_number: int,
                records: list, edits: list, dpi: int = 300, bg: str = "white",
                quality: int = 95, padding_pt: float = 6.0, say=None) -> list:
    """
    Накладывает ручные правки поверх нарезки.

    Вызывается дважды: сразу после правки на сайте (быстро — сегментация не
    переделывается, только рендер страницы) и заново в конце автонарезки, чтобы
    «порезать заново» не стирало ручную работу.

    records меняется на месте и возвращается.
    """
    def note(msg):
        if say:
            say(msg)

    if not edits:
        return records

    apt_dir = out_dir / "apartments"
    apt_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = out_dir / "thumbs"
    hit_path = out_dir / "hitmap.png"
    hit = (np.asarray(Image.open(hit_path).convert("L")).copy()
           if hit_path.exists() else None)

    doc = fitz.open(pdf_path)
    base_img = None
    try:
        page = doc[0]
        by_number = {r["number"]: r for r in records}

        for e in edits:
            action, target, number = e["action"], e["target"], e["number"]

            if action == "delete":
                rec = by_number.pop(target, None)
                if rec is None:
                    continue
                (apt_dir / rec["filename"]).unlink(missing_ok=True)
                (thumb_dir / rec["filename"]).unlink(missing_ok=True)
                if hit is not None:
                    hit[hit == rec["idx"]] = 0
                records.remove(rec)
                note(f"      удалено вручную: {rec['label']}")

            elif action == "rename":
                rec = by_number.get(target)
                if rec is None:
                    continue
                new_name = file_name(project_name, floor_number, number,
                                     Path(rec["filename"]).suffix)
                old = apt_dir / rec["filename"]
                if old.exists():
                    old.replace(apt_dir / new_name)
                old_thumb = thumb_dir / rec["filename"]
                if old_thumb.exists():
                    old_thumb.replace(thumb_dir / new_name)
                by_number.pop(target, None)
                rec["number"], rec["label"], rec["filename"] = number, number, new_name
                by_number[number] = rec
                note(f"      переименовано вручную: {target} -> {number}")

            elif action == "add":
                points = json.loads(e["polygon"] or "[]")
                if len(points) < 3:
                    continue
                if base_img is None:
                    base_img = ca.page_to_image(page, dpi)
                mask = _polygon_mask(points, base_img.width, base_img.height)
                ext = ca.ext_for(bg)
                name = file_name(project_name, floor_number, number, ext)
                pad_px = max(int(round(padding_pt * dpi / 72.0)), 0)
                if not ca.save_apartment(base_img, mask, apt_dir / name,
                                         pad_px, bg, quality):
                    note(f"      ручной контур {number}: пустая область, пропущено")
                    continue
                make_thumb(apt_dir / name, thumb_dir)
                idx = max((r["idx"] for r in records), default=0) + 1
                if hit is not None:
                    small = np.asarray(Image.fromarray(mask).resize(
                        (hit.shape[1], hit.shape[0]), Image.NEAREST))
                    hit[small] = idx
                    ys, xs = np.nonzero(small)
                    box = ((float(xs.min()), float(ys.min()),
                            float(xs.max()), float(ys.max()))
                           if xs.size else (0.0, 0.0, 0.0, 0.0))
                else:
                    box = (0.0, 0.0, 0.0, 0.0)
                rec = {"idx": idx, "label": number, "number": number,
                       "filename": name, "box": box, "manual": True}
                records.append(rec)
                by_number[number] = rec
                note(f"      добавлено вручную: {number}")
    finally:
        doc.close()

    if hit is not None:
        Image.fromarray(hit, mode="L").save(hit_path, optimize=True)
    return records


def cut_floor(pdf_path: Path, out_dir: Path, project_name: str, floor_number: int,
              log=None, dpi: int = 300, bg: str = "white",
              kind: str = "flats", edits: list | None = None) -> dict:
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
    if (out_dir / "thumbs").exists():
        shutil.rmtree(out_dir / "thumbs")

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
            make_thumb(apt_dir / name, out_dir / "thumbs")

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

        # Ручные правки накладываем поверх свежей автонарезки — «порезать
        # заново» не должно стирать то, что человек доделал руками.
        if edits:
            records = apply_edits(pdf_path, out_dir, project_name, floor_number,
                                  records, edits, dpi=dpi, bg=bg,
                                  quality=args.quality, padding_pt=args.padding,
                                  say=say)

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
