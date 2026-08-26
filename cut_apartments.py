# -*- coding: utf-8 -*-
"""
Нарізка планів квартир із загального плану поверху у окремі PNG з прозорим фоном.

Приймає: PDF (векторний або скан) і звичайні картинки (PNG/JPG/TIF/BMP).

Два режими, перемикаються автоматично:
  * vector — підписи квартир беруться як текст PDF, контур квартири — з векторної
    заливки креслення. Найточніший.
  * raster — сторінка/картинка розбирається як зображення: підписи розпізнаються
    OCR (Tesseract), а сама квартира виділяється за кольоровою заливкою, яку
    ріжуть стіни. Працює зі сканами та «мертвими» PDF без тексту.

Приклади:
  python cut_apartments.py
  python cut_apartments.py "plan.pdf" -o out --dpi 300
  python cut_apartments.py scan.jpg --mode raster --prefix К
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi

Image.MAX_IMAGE_PIXELS = None

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# Підпис квартири: "А-12.1", "К-3.15", "A-12,1". Перший символ може не
# розпізнатись зі шрифту PDF (�) або бути прочитаний OCR як латиниця — не біда.
LABEL_RE = re.compile(r"^\s*([^\s\-\u2013]{0,3})[\-\u2013\u2014](\d{1,3})[.,](\d{1,3})\s*$")
# Some plans label an apartment as bare "24.1" (no letter, no dash) - the same
# shape as an area figure ("39.72"). We tell a bare label apart by font size:
# it is always noticeably larger than the area numbers printed under it.
BARE_LABEL_RE = re.compile(r"^\s*(\d{1,3})[.,](\d{1,3})\s*$")
FLOOR_IN_NAME_RE = re.compile(r"(\d{1,3})\s*(?:-?[а-яїієґ]{0,3})?\s*(?:поверх|этаж|floor)", re.I)


@dataclass
class Apartment:
    label: str                     # А-12.1
    floor: str                     # 12
    number: str                    # 1
    point: tuple                   # (x, y) у пунктах сторінки або пікселях картинки
    paths: list = field(default_factory=list)   # векторний режим
    rect: fitz.Rect = None
    mask: np.ndarray = None                     # растровий режим


# =========================================================== геометрія / утиліти
def rect_gap(a: fitz.Rect, b: fitz.Rect) -> float:
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0)
    return (dx * dx + dy * dy) ** 0.5


def rect_overlap_area(a: fitz.Rect, b: fitz.Rect) -> float:
    dx = min(a.x1, b.x1) - max(a.x0, b.x0)
    dy = min(a.y1, b.y1) - max(a.y0, b.y0)
    return dx * dy if dx > 0 and dy > 0 else 0.0


def contact_length(a: fitz.Rect, b: fitz.Rect) -> float:
    """Довжина спільної межі: чим більша, тим імовірніше, що це одна квартира."""
    ox = min(a.x1, b.x1) - max(a.x0, b.x0)
    oy = min(a.y1, b.y1) - max(a.y0, b.y0)
    if ox <= 0 and oy > 0:
        return oy
    if oy <= 0 and ox > 0:
        return ox
    return max(ox, oy, 0.0)


def safe_name(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")


def disk(radius: int) -> np.ndarray:
    if radius <= 0:
        return np.ones((1, 1), bool)
    y, x = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    return x * x + y * y <= radius * radius


def parse_label(text, prefix):
    m = LABEL_RE.match(text)
    if not m:
        return None
    letter, floor, num = m.groups()
    letter = "".join(ch for ch in letter if ch.isalnum() and ch != "\ufffd")
    if not letter or letter.isdigit():
        letter = prefix
    return f"{letter}-{floor}.{num}", floor, num


# ================================================================ векторний режим
def is_apartment_fill(path, min_area, gray_tol=0.006, white_lvl=0.98):
    """Кольорова (не біла і не сіра) заливка достатньої площі."""
    if path["type"] not in ("f", "fs"):
        return False
    fill = path.get("fill")
    if not fill or len(fill) < 3:
        return False
    r, g, b = fill[:3]
    if min(r, g, b) >= white_lvl:
        return False
    if max(r, g, b) - min(r, g, b) <= gray_tol:
        return False
    return path["rect"].get_area() >= min_area


def paths_to_mask(page_rect, paths, dpi, clip=None):
    """Малює контури заливок і повертає булеву маску (True = всередині)."""
    doc = fitz.open()
    pg = doc.new_page(width=page_rect.width, height=page_rect.height)
    shape = pg.new_shape()
    for path in paths:
        drawn = False
        for it in path["items"]:
            op = it[0]
            if op == "l":
                shape.draw_line(it[1], it[2])
            elif op == "c":
                shape.draw_bezier(it[1], it[2], it[3], it[4])
            elif op == "re":
                shape.draw_rect(it[1])
            elif op == "qu":
                shape.draw_quad(it[1])
            else:
                continue
            drawn = True
        if drawn:
            shape.finish(fill=(0, 0, 0), color=None,
                         even_odd=path.get("even_odd", False), closePath=True)
    shape.commit()
    pix = pg.get_pixmap(dpi=dpi, clip=clip, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    doc.close()
    return arr < 128


def _cluster_topleft(items, max_dx=8.0, max_dy=20.0):
    """
    items: list of (floor, num, bbox, size).

    Groups bounding boxes that sit close together - an apartment's label and
    the area figures under it are usually within ~5-15pt vertically - and
    keeps only the top member of each group, that is the label.

    dx and dy use separate, deliberately different limits. A label and its
    own area figures are stacked in the same column (near-zero or negative
    dx, i.e. their x-ranges overlap), so max_dx is tight. Two neighbouring
    apartments packed close together can still land within max_dy of each
    other diagonally; a tight max_dx keeps that diagonal case from bridging
    them into one cluster and swallowing one apartment's label.
    """
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        xi0, yi0, xi1, yi1 = items[i][2]
        for j in range(i + 1, n):
            xj0, yj0, xj1, yj1 = items[j][2]
            dx = max(xi0, xj0) - min(xi1, xj1)
            dy = max(yi0, yj0) - min(yi1, yj1)
            if dx <= max_dx and dy <= max_dy:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out = []
    for members in groups.values():
        # the label is always the largest text in its own info-box. If two
        # apartments' boxes end up merged into one group anyway (their gap
        # can be as small as a label-to-area gap), there will be two members
        # sharing that top font size instead of one - keep both rather than
        # picking a single "top-left" one and losing an apartment.
        top_size = max(items[i][3] for i in members)
        labels = [i for i in members if items[i][3] == top_size]
        for i in labels:
            out.append(items[i])
    return out


def find_text_labels(page, prefix):
    """
    Apartment labels from the PDF text layer.

    Most plans write "A-12.1" (letter, dash, floor, number) - unambiguous,
    taken as-is. Some plans instead write a bare "24.1", which has the exact
    same shape as an area figure ("39.72"). To tell them apart: drop
    whatever bare number sits at the page's single most common font size
    (that is always the dimension-line callouts scattered over the plan,
    vastly outnumbering real labels); what remains is grouped by proximity
    into apartment info-boxes (label plus its one or two area lines), and
    only the label - the top-left item of each group - is kept.
    """
    trusted, bare = [], []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"]
                parsed = parse_label(text, prefix)
                if parsed:
                    label, floor, num = parsed
                    x0, y0, x1, y1 = span["bbox"]
                    trusted.append(Apartment(label, floor, num,
                                             ((x0 + x1) / 2, (y0 + y1) / 2)))
                    continue
                bm = BARE_LABEL_RE.match(text)
                if bm:
                    bare.append((bm.group(1), bm.group(2), span["bbox"],
                                round(span["size"], 1)))

    if not bare:
        return trusted

    counts = {}
    for *_, sz in bare:
        counts[sz] = counts.get(sz, 0) + 1
    noise_size = max(counts, key=counts.get)   # dimension callouts: the most common size
    bare = [b for b in bare if b[3] > noise_size + 0.3]

    for floor, num, bbox, _sz in _cluster_topleft(bare):
        x0, y0, x1, y1 = bbox
        trusted.append(Apartment(f"{prefix}-{floor}.{num}", floor, num,
                                 ((x0 + x1) / 2, (y0 + y1) / 2)))
    return trusted


def vector_apartments(page, labels, args):
    """Збирає квартири з векторних заливок. Повертає (квартири, без_заливки, сироти)."""
    fills = [p for p in page.get_drawings() if is_apartment_fill(p, args.min_area)]
    if not fills:
        return [], [a.label for a in labels], 0, 0

    unassigned = []
    for path in fills:                       # тіло = заливка, в якій стоїть підпис
        r = path["rect"]
        inside = [a for a in labels
                  if not a.paths and r.contains(fitz.Point(*a.point))]
        owner = None
        if inside:
            m = paths_to_mask(page.rect, [path], 72)
            h, w = m.shape
            for a in inside:
                x, y = int(a.point[0]), int(a.point[1])
                if 0 <= x < w and 0 <= y < h and m[y, x]:
                    owner = a
                    break
        (owner.paths if owner else unassigned).append(path)

    # Деякі плани малюють підпис за 1-3pt від межі його заливки, а не строго
    # всередині (частіше трапляється в кутових приміщеннях зі скошеними
    # стінами). Для підписів, яким не дісталось власної заливки, пробуємо
    # ще раз з невеликим допуском - тільки серед ще не розібраних заливок.
    stray = [a for a in labels if not a.paths]
    if stray:
        tol = 3.0
        for path in list(unassigned):
            r = path["rect"]
            near = [a for a in stray if not a.paths
                   and rect_gap(fitz.Rect(a.point, a.point), r) <= tol]
            if near:
                near[0].paths.append(path)
                unassigned.remove(path)

    bodies = [a for a in labels if a.paths]
    for a in bodies:
        a.rect = union_rect(a.paths)

    # Балкони/лоджії -> до своєї квартири. Приєднуємо глобально найближчий
    # шматок на кожному кроці й одразу перераховуємо межі квартири, перш ніж
    # шукати наступний: інакше відстань міряють від початкового крихітного
    # фрагмента, а не від фактичної форми квартири, і далекий шматок може
    # "проскочити" лише тому, що випадково опинився поруч з якимось іншим
    # уже приєднаним фрагментом.
    #
    # Запобіжник: у щільній забудові (кутові студії на скісному фасаді тощо)
    # жадібне приєднання інакше може ланцюжком підхопити чужі кімнати одну
    # за одною й роздути квартиру в рази - обмежуємо приєднану площу
    # відносно того, що квартирі реально належить від початку.
    own_area = {id(a): sum(pp["rect"].get_area() for pp in a.paths) for a in bodies}
    added_area = {id(a): 0.0 for a in bodies}
    area_cap = 2.0

    remaining = list(unassigned)
    while remaining:
        best_i, best_a, best_score = None, None, None
        for i, path in enumerate(remaining):
            r = path["rect"]
            area = r.get_area()
            for a in bodies:
                if added_area[id(a)] + area > own_area[id(a)] * area_cap:
                    continue
                gap = rect_gap(r, a.rect)
                ovl = rect_overlap_area(r, a.rect)
                if ovl <= 0 and gap > args.attach_gap:
                    continue
                score = (ovl, contact_length(r, a.rect), -gap)
                if best_score is None or score > best_score:
                    best_i, best_a, best_score = i, a, score
        if best_a is None:
            break
        picked = remaining.pop(best_i)
        best_a.paths.append(picked)
        best_a.rect = union_rect(best_a.paths)
        added_area[id(best_a)] += picked["rect"].get_area()
    orphans = len(remaining)

    for a in bodies:
        a.rect = union_rect(a.paths)
    missing = [a.label for a in labels if not a.paths]
    return bodies, missing, orphans, len(fills)


def union_rect(paths):
    r = fitz.Rect(paths[0]["rect"])
    for p in paths[1:]:
        r |= p["rect"]
    return r


# ================================================================ растровий режим
def find_tesseract(explicit=None):
    if explicit:
        return explicit if Path(explicit).exists() else None
    exe = shutil.which("tesseract")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
              r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
              "/usr/bin/tesseract", "/usr/local/bin/tesseract",
              "/opt/homebrew/bin/tesseract"):
        if Path(p).exists():
            return p
    return None


# у результатах OCR підпис часто злипається зі сміттям: "A-12.2 «", "A- 12.151"
OCR_LABEL_RE = re.compile(
    r"(?<![\d,.])([A-ZА-ЯЇІЄҐ]?)\s*[-–—]\s*(\d{1,3})\s*[.,]\s*(\d{1,3})(?![\d,.])")


def clean_for_ocr(img: Image.Image, px_per_pt: float) -> Image.Image:
    """Прибирає з креслення стіни, меблі й довгі лінії — лишає тільки текст."""
    g = np.asarray(img.convert("L"))
    ink = g < 160
    lab, n = ndi.label(ink, structure=np.ones((3, 3), bool))
    if not n:
        return img.convert("L")
    max_side = max(int(round(32 * px_per_pt)), 20)     # ~32 пункти — стеля для літери
    long_line = max(int(round(11 * px_per_pt)), 12)
    thin = max(int(round(1.2 * px_per_pt)), 2)
    keep = np.zeros(n + 1, bool)
    for i, sl in enumerate(ndi.find_objects(lab), start=1):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        if h > max_side or w > max_side:
            continue
        if (w >= long_line and h <= thin) or (h >= long_line and w <= thin):
            continue
        keep[i] = True
    return Image.fromarray(np.where(keep[lab], 0, 255).astype(np.uint8))


def run_tesseract(img: Image.Image, tess: str, psm: str, upscale: float):
    if upscale != 1.0:
        img = img.resize((int(img.width * upscale), int(img.height * upscale)),
                         Image.LANCZOS)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "page.png"
        img.convert("L").save(src)
        try:
            res = subprocess.run([tess, str(src), "stdout", "--psm", psm, "tsv"],
                                 capture_output=True, timeout=900)
        except Exception as exc:                       # noqa: BLE001
            print(f"      !! OCR не запустився: {exc}")
            return []
    if res.returncode != 0:
        return []
    lines = {}
    rows = csv.DictReader(io.StringIO(res.stdout.decode("utf-8", "replace")),
                          delimiter="	", quoting=csv.QUOTE_NONE)
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            if float(row.get("conf", -1) or -1) < 25:
                continue
            box = (int(row["left"]), int(row["top"]),
                   int(row["width"]), int(row["height"]))
            key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
        except (KeyError, ValueError):
            continue
        lines.setdefault(key, []).append((text, box))

    found = []
    for words in lines.values():
        text, spans = "", []
        for w, box in words:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w), box))
            text += w
        for m in OCR_LABEL_RE.finditer(text):
            hit = [b for s0, s1, b in spans if s0 < m.end() and s1 > m.start()]
            if not hit:
                continue
            x = (min(b[0] for b in hit) + max(b[0] + b[2] for b in hit)) / 2 / upscale
            y = (min(b[1] for b in hit) + max(b[1] + b[3] for b in hit)) / 2 / upscale
            found.append((m.groups(), (x, y)))
    return found


LATIN2CYR = {"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К",
             "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У"}


def fix_letter(letter, prefix, keep_latin=False):
    if not letter or letter.isdigit():
        return prefix
    return letter if keep_latin else LATIN2CYR.get(letter, letter)


def prune_labels(labels, px_per_pt):
    """Прибирає сміття OCR: чужий поверх і зайву цифру ("A-12.151" при "A-12.15")."""
    if not labels:
        return labels
    floors = {}
    for a in labels:
        floors[a.floor] = floors.get(a.floor, 0) + 1
    main_floor = max(floors, key=lambda f: floors[f])
    labels = [a for a in labels if a.floor == main_floor]

    near = 120 * px_per_pt
    nums = {a.number: a for a in labels}
    out = []
    for a in labels:
        short = a.number[:-1]
        twin = nums.get(short)
        if len(a.number) >= 3 and twin is not None:
            d = ((a.point[0] - twin.point[0]) ** 2 + (a.point[1] - twin.point[1]) ** 2) ** 0.5
            if d < near:
                continue
        out.append(a)
    return out


def ocr_labels(img: Image.Image, tess: str, prefix: str, px_per_pt: float, tiles: int = 3):
    """
    Розпізнає підписи квартир. Кілька проходів із різними налаштуваннями:
    на щільному кресленні жоден окремий прохід не бачить усе.
    """
    cleaned = clean_for_ocr(img, px_per_pt)
    out, seen = [], set()

    def take(results, dx=0, dy=0):
        for (letter, floor, num), point in results:
            key = (floor, num)
            if key in seen:
                continue
            seen.add(key)
            out.append(Apartment(f"{fix_letter(letter, prefix)}-{floor}.{num}",
                                 floor, num, (point[0] + dx, point[1] + dy)))

    for src, up, psm in ((cleaned, 1.5, "11"), (cleaned, 1.0, "3"),
                         (img, 1.0, "11"), (cleaned, 2.0, "11")):
        take(run_tesseract(src, tess, psm, up))

    if tiles > 1:                     # по шматках знаходиться те, що загубилось на цілій сторінці
        w, h = cleaned.size
        ov = 0.15
        for r in range(tiles):
            for c in range(tiles):
                x0 = max(int(c * w / tiles - ov * w / tiles), 0)
                y0 = max(int(r * h / tiles - ov * h / tiles), 0)
                x1 = min(int((c + 1) * w / tiles + ov * w / tiles), w)
                y1 = min(int((r + 1) * h / tiles + ov * h / tiles), h)
                take(run_tesseract(cleaned.crop((x0, y0, x1, y1)), tess, "11", 1.5),
                     x0, y0)

    return prune_labels(out, px_per_pt)


def colored_fill_mask(rgb: np.ndarray, sat_thr: float, dark_thr: float):
    """Кольорова заливка квартир (без стін і ліній)."""
    arr = rgb.astype(np.float32) / 255.0
    mx = arr.max(2)
    mn = arr.min(2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    colored = (sat > sat_thr) & (mx > 0.35)
    dark = mx < dark_thr
    return colored & ~dark


def seed_component(lab_img: np.ndarray, seed, radius: int):
    """Компонент, у якому стоїть підпис: беремо не один піксель, а віконце навколо."""
    y, x = int(round(seed.point[1])), int(round(seed.point[0]))
    r = max(radius, 3)
    y0, y1 = max(y - r, 0), min(y + r + 1, lab_img.shape[0])
    x0, x1 = max(x - r, 0), min(x + r + 1, lab_img.shape[1])
    win = lab_img[y0:y1, x0:x1].ravel()
    win = win[win > 0]
    if win.size == 0:
        return 0
    return int(np.bincount(win).argmax())


def room_edges(rooms: np.ndarray, n: int, max_gap: float):
    """
    Для кожної пари сусідніх кімнат — товщина стіни між ними (у пікселях).
    Далі кімнати зливаються в квартиру, починаючи з найтонших перегородок.
    """
    if n < 2:
        return []
    dist, (iy, ix) = ndi.distance_transform_edt(rooms == 0, return_indices=True)
    near = rooms[iy, ix]

    keys, gaps = [], []
    for a, b, wa, wb in ((near[:, :-1], near[:, 1:], dist[:, :-1], dist[:, 1:]),
                         (near[:-1, :], near[1:, :], dist[:-1, :], dist[1:, :])):
        m = (a != b) & (a > 0) & (b > 0)
        if not m.any():
            continue
        av, bv = a[m], b[m]
        keys.append(np.minimum(av, bv).astype(np.int64) * (n + 1)
                    + np.maximum(av, bv).astype(np.int64))
        gaps.append(wa[m] + wb[m] + 1.0)
    if not keys:
        return []
    key = np.concatenate(keys)
    gap = np.concatenate(gaps)
    uk, inv = np.unique(key, return_inverse=True)
    best = np.full(uk.size, np.inf)
    np.minimum.at(best, inv, gap)

    out = []
    for k, g in zip(uk, best):
        if g > max_gap:
            continue
        out.append((float(g), int(k // (n + 1)), int(k % (n + 1))))
    out.sort()
    return out


def group_rooms(rooms, n, edges, seed_of_room):
    """
    Зливає кімнати в квартири по зростанню товщини стіни між ними.
    Дві кімнати з РІЗНИМИ підписами ніколи не зливаються — це різні квартири.
    """
    parent = list(range(n + 1))
    seed = dict(seed_of_room)                 # корінь -> квартира

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for gap, a, b in edges:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        sa, sb = seed.get(ra), seed.get(rb)
        if sa is not None and sb is not None and sa is not sb:
            continue                          # дві різні квартири — не чіпаємо
        parent[rb] = ra
        if seed.pop(rb, None) is not None or sa is not None:
            seed[ra] = sa if sa is not None else sb
        elif sb is not None:
            seed[ra] = sb
    groups, root_of = {}, {}
    for r in range(1, n + 1):
        root = find(r)
        root_of[r] = root
        groups.setdefault(root, []).append(r)
    return groups, seed, root_of


def raster_apartments(rgb: np.ndarray, labels: list, args, px_per_pt: float, namer=None):
    """Виділяє маску кожної квартири на растрі. Маски — в координатах rgb."""
    fill = colored_fill_mask(rgb, args.sat, args.dark)
    coverage = float(fill.mean())
    if coverage < 0.01:
        return [], [a.label for a in labels], 0, coverage
    fill = ndi.binary_opening(fill, structure=disk(1))

    rooms, n = ndi.label(fill)
    if n == 0:
        return [], [a.label for a in labels], 0, coverage
    sizes = np.bincount(rooms.ravel(), minlength=n + 1)

    # підпис намальований поверх заливки, тож сам піксель підпису «не залитий»
    h, w = fill.shape
    _, (iy, ix) = ndi.distance_transform_edt(~fill, return_indices=True)
    seed_of_room, seeds = {}, []
    for a in labels:
        x, y = int(round(a.point[0])), int(round(a.point[1]))
        if not (0 <= x < w and 0 <= y < h):
            continue
        if not fill[y, x]:
            x, y = int(ix[y, x]), int(iy[y, x])
            a.point = (x, y)
        r = seed_component(rooms, a, int(round(4 * px_per_pt)))
        if not r or r in seed_of_room:
            continue
        seed_of_room[r] = a
        seeds.append(a)

    # кімнати однієї квартири розділені тонкою перегородкою, сусідні квартири —
    # товстою стіною, тому зливаємо тільки через тонке
    merge_gap = max(args.close * px_per_pt, 2.0)
    attach_px = max(args.attach_gap * px_per_pt, merge_gap)
    far_edges = room_edges(rooms, n, attach_px)
    near_edges = [e for e in far_edges if e[0] <= merge_gap]
    groups, seed, root_of = group_rooms(rooms, n, near_edges, seed_of_room)

    bodies = []
    for root, members in groups.items():
        a = seed.get(root)
        if a is None:
            continue
        a.mask = np.isin(rooms, members)
        bodies.append(a)
    if not bodies:
        return [], [a.label for a in labels], 0, coverage

    # шматки без підпису склеюємо між собою (але не з чужими квартирами):
    # так квартира, номер якої не прочитався, збереться докупи, а не розтягнеться
    # по сусідах
    free = {r: r for r in groups if seed.get(r) is None}

    def find2(x):
        while free[x] != x:
            free[x] = free[free[x]]
            x = free[x]
        return x

    for _gap, a, b in far_edges:
        ra, rb = root_of[a], root_of[b]
        if ra == rb or ra not in free or rb not in free:
            continue
        fa, fb = find2(ra), find2(rb)
        if fa != fb:
            free[fb] = fa
    clusters = {}
    for root in list(free):
        clusters.setdefault(find2(root), []).extend(groups[root])
    for root, members in list(groups.items()):
        if seed.get(root) is None:
            groups.pop(root)
    groups.update(clusters)

    med = float(np.median([a.mask.sum() for a in bodies]))
    extra, orphans = 0, 0
    leftovers = []
    for root, members in groups.items():
        if seed.get(root) is not None:
            continue
        area = int(sum(sizes[m] for m in members))
        if area >= 0.2 * med:                 # квартира, підпис якої не прочитався
            piece = np.isin(rooms, members)
            extra += 1
            label, floor, num = None, bodies[0].floor, str(900 + extra)
            if namer is not None:
                got = namer(piece)
                if got and got[0] not in {b.label for b in bodies}:
                    label, floor, num = got
            ys, xs = np.nonzero(piece)
            a = Apartment(label or f"без_номера_{extra}", floor, num,
                          (float(xs.mean()), float(ys.mean())))
            a.mask = piece
            bodies.append(a)
            labels.append(a)
        else:
            leftovers.append(members)

    if extra:
        unnamed = sum(1 for a in bodies if a.label.startswith("без_номера"))
        print(f"      i  знайдено ще {extra} квартир(и) поза розпізнаними підписами"
              + (f", з них {unnamed} без номера — перейменуйте вручну" if unnamed else ""))

    # дрібні шматки (балкони, лоджії) -> до найближчої квартири
    if leftovers:
        owned = np.zeros(fill.shape, np.int32)
        for i, a in enumerate(bodies, start=1):
            owned[a.mask] = i
        dist2, (jy, jx) = ndi.distance_transform_edt(owned == 0, return_indices=True)
        near_owner = owned[jy, jx]
        for members in leftovers:
            piece = np.isin(rooms, members)
            cand = near_owner[piece]
            d = dist2[piece]
            cand = cand[d <= attach_px]
            if cand.size == 0:
                orphans += 1
                continue
            winner = int(np.bincount(cand).argmax())
            if winner == 0:
                orphans += 1
                continue
            bodies[winner - 1].mask = bodies[winner - 1].mask | piece

    missing = [a.label for a in labels if a.mask is None or not a.mask.any()]
    bodies = [a for a in bodies if a.mask is not None and a.mask.any()]
    return bodies, missing, orphans, coverage


def ink_mask(rgb: np.ndarray, white_lvl: int = 244) -> np.ndarray:
    """Усе, що намальовано: лінії, стіни, меблі, символи, текст (не білий фон)."""
    return rgb.min(2) < white_lvl


def square(n: int) -> np.ndarray:
    n = max(int(n), 1)
    return np.ones((n, n), bool)


def orthogonalize(mask: np.ndarray, cell: int) -> np.ndarray:
    """Підганяє контур під сітку: межа йде тільки по горизонталі й вертикалі."""
    cell = max(int(cell), 1)
    if cell == 1:
        return mask
    h, w = mask.shape
    ph, pw = (-h) % cell, (-w) % cell
    m = np.pad(mask, ((0, ph), (0, pw)))
    m = m.reshape(m.shape[0] // cell, cell, m.shape[1] // cell, cell).any(axis=(1, 3))
    up = np.repeat(np.repeat(m, cell, axis=0), cell, axis=1)
    return up[:h, :w]


def smooth_mask(mask: np.ndarray, radius: int, protect: np.ndarray = None) -> np.ndarray:
    """
    Робить контур рівним і прямокутним.

    Спершу зрізаються тонкі «вусики» — дуги дверей, виносні лінії розмірів,
    хвости штриховки: саме вони давали сходинки на межі. Далі контур
    згладжується квадратним елементом (креслення ортогональне, тож кути
    лишаються кутами) і підганяється під сітку.
    """
    if radius <= 0:
        return ndi.binary_fill_holes(mask)
    m = ndi.binary_opening(mask, structure=square(max(radius * 2, 6)))
    if protect is not None:
        m = m | protect
    m = ndi.binary_closing(m, structure=square(radius * 2 + 1))
    m = ndi.binary_fill_holes(m)
    m = orthogonalize(m, max(radius // 2, 2))
    return ndi.binary_fill_holes(m)


def add_door_swings(mask, ink_solid, reach_px, max_area_px):
    """
    Повертає в кадр вхідні двері: область, обмежену дугою відчинення, полотном
    дверей і стіною, додаємо цілком. Межа в цьому місці йде по самій дузі —
    півколом, як на кресленні, і вже не вирівнюється по сітці.
    """
    reach = max(int(reach_px), 1)
    zone = ndi.binary_dilation(mask, structure=square(3), iterations=reach)
    closed = ndi.binary_fill_holes(mask | (ink_solid & zone))
    cand = closed & ~mask
    if not cand.any():
        return mask
    lab, n = ndi.label(cand, structure=np.ones((3, 3), bool))
    if not n:
        return mask
    sizes = np.bincount(lab.ravel(), minlength=n + 1)
    boxes = ndi.find_objects(lab)
    touching = np.unique(lab[ndi.binary_dilation(mask, structure=np.ones((3, 3), bool))])
    keep = []
    for i in touching:
        if i <= 0 or not (4 <= sizes[i] <= max_area_px):
            continue
        sl = boxes[i - 1]
        if sl is None:
            continue
        bh = sl[0].stop - sl[0].start
        bw = sl[1].stop - sl[1].start
        if bh > 2 * reach or bw > 2 * reach:        # довга лінія — не двері
            continue
        if sizes[i] < 0.2 * bh * bw:                # рідкий контур — теж не двері
            continue
        keep.append(i)
    if not keep:
        return mask
    return mask | np.isin(lab, keep)


def solid_blocks(rgb: np.ndarray) -> np.ndarray:
    """Суцільні залиті блоки (вентшахти, стовпи) — сірі плями, не штриховка."""
    lum = rgb.astype(np.float32).mean(2) / 255.0
    blocks = (lum > 0.18) & (lum < 0.8)
    return ndi.binary_opening(blocks, structure=square(3))


def expand_masks(masks, rgb, dilate_px, snap_area_px, smooth_px):
    """
    Добудовує маски квартир до готового кадру.

    1. Маски ростуть одночасно вздовж намальованого (стіни, лінії), тому не
       вилазять у порожній коридор, а на спільній стіні зустрічаються рівно
       посередині — межа виходить пряма, по стіні.
    2. Кожен цілісний елемент (вентблок, символ вікна, сантехніка, шафа)
       потрапляє у кадр ЦІЛКОМ або не потрапляє зовсім — залежно від того,
       чиєї це квартири більша частина. Половинок не буває.
    3. Контур згладжується, дірки всередині зашиваються.
    """
    if not masks:
        return masks
    shape = masks[0].shape
    ink = ink_mask(rgb)
    ink_solid = ndi.binary_closing(ink, structure=disk(2))

    lab = np.zeros(shape, np.int32)
    for i, m in enumerate(masks, start=1):
        lab[m & (lab == 0)] = i

    steps = max(int(round(dilate_px)), 0)
    if steps:
        near = ndi.binary_dilation(lab > 0, structure=disk(2))
        allowed = ink_solid | near
        st = np.ones((3, 3), bool)
        for _ in range(steps):
            grown = ndi.grey_dilation(lab, footprint=st)
            fresh = (lab == 0) & allowed & (grown > 0)
            if not fresh.any():
                break
            lab[fresh] = grown[fresh]

    out = [(lab == i) | masks[i - 1] for i in range(1, len(masks) + 1)]

    # зона стіни навколо квартири: далі неї нічого не добираємо, інакше в кадр
    # лізуть дуги дверей і виносні лінії, а межа перетворюється на «сходинки»
    envelopes = [ndi.binary_dilation(m, structure=square(3), iterations=max(steps, 1))
                 for m in masks]

    # елемент — цілком свій або зовсім чужий
    parts = [(ink, snap_area_px)]
    blocks = solid_blocks(rgb)
    if blocks.any():
        parts.append((blocks, snap_area_px))
    for layer, cap in parts:
        out = all_or_nothing(out, layer, cap, envelopes)

    door_reach = max(int(round(dilate_px * 2)), 1)
    door_area = max(snap_area_px, 1.0)
    result = []
    for i, m in enumerate(out):
        clean = smooth_mask(m, int(round(smooth_px)), protect=masks[i])
        result.append(add_door_swings(clean, ink_solid, door_reach, door_area))
    return result


def all_or_nothing(out, layer, snap_area_px, envelopes):
    """
    Кожен цілісний елемент або повністю в кадрі, або повністю поза ним.
    Береться лише те, що вміщається в зону стіни навколо квартири.
    """
    comp, n = ndi.label(layer, structure=np.ones((3, 3), bool))
    if n and snap_area_px > 0:
        sizes = np.bincount(comp.ravel(), minlength=n + 1).astype(np.float64)
        small = sizes <= snap_area_px
        small[0] = False
        for i, m in enumerate(out):
            sel = comp[m]
            if sel.size == 0:
                continue
            hit = np.bincount(sel, minlength=n + 1).astype(np.float64)
            share = np.zeros_like(hit)
            np.divide(hit, sizes, out=share, where=sizes > 0)
            outside = np.bincount(comp[~envelopes[i]], minlength=n + 1)
            fits = outside == 0                                  # не стирчить за стіну
            mine = np.nonzero(small & fits & (share >= 0.4))[0]  # беремо повністю
            theirs = np.nonzero(small & (hit > 0) & ~(fits & (share >= 0.4)))[0]
            if theirs.size:
                m = m & ~np.isin(comp, theirs)
            if mine.size:
                m = m | np.isin(comp, mine)
            out[i] = m
    return out


# ========================================================================= вивід
def save_apartment(base_img: Image.Image, mask: np.ndarray, out_path: Path,
                   pad_px: int, bg: str, quality: int):
    m_img = Image.fromarray((mask * 255).astype(np.uint8), mode="L")
    if m_img.size != base_img.size:                # BILINEAR = край без сходинок
        m_img = m_img.resize(base_img.size, Image.BILINEAR)

    box = m_img.getbbox()
    if not box:
        return None
    box = (max(box[0] - pad_px, 0), max(box[1] - pad_px, 0),
           min(box[2] + pad_px, base_img.width), min(box[3] + pad_px, base_img.height))
    crop = base_img.crop(box)
    m_crop = m_img.crop(box)

    if bg == "transparent":
        res = crop.convert("RGBA")
        res.putalpha(m_crop)
        res.save(out_path, optimize=True)
    elif bg == "jpg":
        canvas = Image.new("RGB", crop.size, (255, 255, 255))
        canvas.paste(crop, (0, 0), m_crop)
        canvas.save(out_path, quality=quality, subsampling=0)
    else:
        canvas = Image.new("RGB", crop.size, (255, 255, 255))
        canvas.paste(crop, (0, 0), m_crop)
        canvas.save(out_path, optimize=True)
    return out_path


def out_name(args, floor_tag, apt, ext, used=None):
    base = safe_name(args.name.format(floor=floor_tag, label=apt.label,
                                      number=apt.number))
    name = base + ext
    if used is not None:
        k = 2
        while name in used:
            name = f"{base}_{k}{ext}"
            k += 1
        used.add(name)
    return name


def ext_for(bg):
    return ".jpg" if bg == "jpg" else ".png"


# ==================================================================== обробка
def floor_from_name(path: Path):
    m = FLOOR_IN_NAME_RE.search(path.stem)
    return m.group(1) if m else None


def page_to_image(page, dpi):
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def cut_page(page, img_getter, args, tess, page_no=1, name_floor=None, log=print):
    """
    Розбирає одну сторінку й повертає (квартири, маски, інфо).

    Маски — булеві масиви в роздільності args.mask_dpi. Збереження файлів
    лишається за викликачем: цим користуються і консоль, і веб-застосунок.
    """
    prefix = args.prefix
    labels, source = [], ""
    info = {"mode": None, "missing": [], "orphans": 0, "labels": 0,
            "floor": None, "mask_dpi": None, "error": None}

    if page is not None and args.mode in ("auto", "vector"):
        labels = find_text_labels(page, prefix)
        source = "текст PDF"

    apts, missing, orphans, mode_used = [], [], 0, None
    if labels and args.mode in ("auto", "vector"):
        apts, missing, orphans, nfills = vector_apartments(page, labels, args)
        if apts:
            mode_used = "vector"
            log(f"    режим vector: підписів {len(labels)} ({source}), "
                f"заливок {nfills}, квартир {len(apts)}")

    if mode_used is None and args.mode != "vector":
        work_dpi = args.work_dpi
        img_work = img_getter(work_dpi)
        px_per_pt = work_dpi / 72.0
        for a in labels:                       # координати з PDF -> пікселі
            a.paths, a.rect = [], None
            a.point = (a.point[0] * px_per_pt, a.point[1] * px_per_pt)
        if not labels:
            if not tess:
                log("    !! Tesseract не знайдено — підписи квартир розпізнати нічим.")
            else:
                log("    розпізнаю підписи квартир (OCR)…")
                labels = ocr_labels(img_work, tess, prefix, px_per_pt, args.ocr_tiles)
                source = "OCR"
        if not labels:
            log("    !! підписів квартир не знайдено — сторінку пропущено")
            info["error"] = "no-labels"
            return [], [], info
        rgb = np.asarray(img_work.convert("RGB"))
        cleaned = clean_for_ocr(img_work, px_per_pt) if tess else None

        def namer(piece, _c=cleaned, _p=px_per_pt):
            """Пробує прочитати номер квартири всередині знайденої області."""
            if _c is None:
                return None
            ys, xs = np.nonzero(piece)
            m = int(round(6 * _p))
            box = (max(int(xs.min()) - m, 0), max(int(ys.min()) - m, 0),
                   min(int(xs.max()) + m, _c.width), min(int(ys.max()) + m, _c.height))
            crop = _c.crop(box)
            for up, psm in ((2.0, "11"), (1.5, "11"), (1.0, "3")):
                for (letter, floor, num), point in run_tesseract(crop, tess, psm, up):
                    x, y = int(point[0]) + box[0], int(point[1]) + box[1]
                    if 0 <= y < piece.shape[0] and 0 <= x < piece.shape[1]:
                        letter = fix_letter(letter, args.prefix)
                        return f"{letter}-{floor}.{num}", floor, num
            return None

        apts, missing, orphans, cov = raster_apartments(rgb, labels, args, px_per_pt,
                                                        namer=namer)
        if not apts:
            log(f"    !! кольорових заливок квартир не знайдено "
                f"(зафарбовано {cov*100:.1f}% сторінки) — сторінку пропущено")
            info["error"] = "no-fills"
            return [], [], info
        mode_used = "raster"
        log(f"    режим raster: підписів {len(labels)} ({source}), "
            f"зафарбовано {cov*100:.1f}%, квартир {len(apts)}")

    if not apts:
        log("    !! квартир не знайдено — сторінку пропущено")
        info["error"] = "no-apartments"
        return [], [], info

    apts = sorted(apts, key=lambda a: (int(a.floor or 0), int(a.number or 0)))

    mask_dpi = min(args.dpi, args.mask_dpi)
    if mode_used == "vector":
        masks = [paths_to_mask(page.rect, a.paths, mask_dpi) for a in apts]
    else:
        k = mask_dpi / args.work_dpi
        masks = [a.mask for a in apts]
        if abs(k - 1.0) > 1e-6:
            size = (max(int(round(masks[0].shape[1] * k)), 1),
                    max(int(round(masks[0].shape[0] * k)), 1))
            masks = [np.asarray(Image.fromarray(m).resize(size, Image.NEAREST))
                     for m in masks]
    mask_rgb = np.asarray(img_getter(mask_dpi).convert("RGB"))
    if mask_rgb.shape[:2] != masks[0].shape:
        masks = [np.asarray(Image.fromarray(m).resize(
            (mask_rgb.shape[1], mask_rgb.shape[0]), Image.NEAREST)) for m in masks]
    scale = mask_dpi / 72.0
    masks = expand_masks(masks, mask_rgb, args.dilate * scale,
                         args.snap_area * scale * scale, args.smooth * scale)

    info.update({"mode": mode_used, "missing": missing, "orphans": orphans,
                 "labels": len(labels), "mask_dpi": mask_dpi,
                 "floor": args.floor or name_floor or apts[0].floor or str(page_no)})
    return apts, masks, info


def process_page(page, img_getter, page_no, out_dir, args, tess, name_floor):
    """Консольний режим: ріже сторінку й зберігає файли."""
    apts, masks, info = cut_page(page, img_getter, args, tess, page_no, name_floor)
    if not apts:
        return 0

    floor_tag = info["floor"]
    base_img = img_getter(args.dpi)
    pad_px = max(int(round(args.padding * args.dpi / 72.0)), 0)
    ext = ext_for(args.bg)

    saved, used = 0, set()
    for apt, mask in zip(apts, masks):
        path = save_apartment(base_img, mask,
                              out_dir / out_name(args, floor_tag, apt, ext, used),
                              pad_px, args.bg, args.quality)
        if path:
            saved += 1
            print(f"      {apt.label:<10} -> {path.name}")
        else:
            print(f"      {apt.label:<10} !! порожня маска, пропущено")

    if info["missing"]:
        print(f"      !! без заливки (не вирізано): {', '.join(info['missing'])}")
    if info["orphans"]:
        print(f"      i  залишились незакріплені заливки ({info['orphans']}) — зазвичай "
              f"це легенда/умовні позначення; якщо це балкони, збільшіть --attach-gap")
    return saved


def process_file(path: Path, out_dir: Path, args, tess):
    print(f"\n{path.name}")
    name_floor = floor_from_name(path)
    total = 0

    if path.suffix.lower() in IMAGE_EXT:
        img = Image.open(path).convert("RGB")
        dpi_guess = args.work_dpi

        def getter(dpi, _img=img, _base=dpi_guess):
            if dpi == _base:
                return _img
            k = dpi / _base
            return _img.resize((max(int(_img.width * k), 1), max(int(_img.height * k), 1)),
                               Image.LANCZOS)

        total += process_page(None, getter, 1, out_dir, args, tess, name_floor)
        return total

    doc = fitz.open(path)
    for pno, page in enumerate(doc, start=1):
        print(f"  сторінка {pno}")
        total += process_page(page, lambda dpi, p=page: page_to_image(p, dpi),
                              pno, out_dir, args, tess, name_floor)
    doc.close()
    return total


def build_parser():
    """Опис усіх ключів — використовують і консоль, і веб-застосунок."""
    ap = argparse.ArgumentParser(
        description="Нарізка квартир із загального плану поверху (PDF/скан -> PNG).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("input", nargs="?", default=".", help="PDF, картинка або папка")
    ap.add_argument("-o", "--out", default="output", help="папка для результатів")
    ap.add_argument("--dpi", type=int, default=300, help="роздільна здатність результату")
    ap.add_argument("--bg", choices=["white", "transparent", "jpg"], default="white",
                    help="фон: прозорий PNG, білий PNG або JPG")
    ap.add_argument("--quality", type=int, default=92, help="якість для --bg jpg")
    ap.add_argument("--mode", choices=["auto", "vector", "raster"], default="auto",
                    help="як розбирати план")
    ap.add_argument("--work-dpi", type=int, default=200,
                    help="роздільна здатність аналізу в режимі raster")
    ap.add_argument("--dilate", type=float, default=16.0,
                    help="наскільки маска доростає вздовж ліній креслення, пункти")
    ap.add_argument("--smooth", type=float, default=4.0,
                    help="згладжування контуру квартири в пунктах (0 — вимкнути)")
    ap.add_argument("--snap-area", type=float, default=3000.0,
                    help="макс. площа (кв. пункти) цілісного елемента, який добирається повністю")
    ap.add_argument("--mask-dpi", type=int, default=150,
                    help="роздільна здатність, на якій рахується межа квартири")
    ap.add_argument("--padding", type=float, default=6.0, help="поле навколо квартири, пункти")
    ap.add_argument("--attach-gap", type=float, default=25.0,
                    help="макс. відстань (пункти), на якій балкон вважається частиною квартири")
    ap.add_argument("--min-area", type=float, default=300.0,
                    help="vector: мін. площа заливки (кв. пункти)")
    ap.add_argument("--close", type=float, default=5.0,
                    help="raster: товщина внутрішньої перегородки в пунктах — "
                         "через тонше кімнати зливаються в одну квартиру, "
                         "через товще (міжквартирна стіна) вже ні")
    ap.add_argument("--sat", type=float, default=0.06,
                    help="raster: поріг насиченості кольору заливки (0..1)")
    ap.add_argument("--dark", type=float, default=0.45,
                    help="raster: поріг темряви для ліній і стін (0..1)")
    ap.add_argument("--ocr-tiles", type=int, default=3,
                    help="raster: скільки шматків на бік розпізнавати додатково "
                         "(1 — вимкнути, швидше, але знайде менше підписів)")
    ap.add_argument("--tesseract", default=None, help="шлях до tesseract.exe")
    ap.add_argument("--prefix", default="А",
                    help="літера в номері квартири, якщо шрифт/OCR її не дає")
    ap.add_argument("--floor", default=None, help="примусово задати номер поверху")
    ap.add_argument("--name", default="{floor}поверх_{label}",
                    help="шаблон імені файлу: {floor}, {label}, {number}")
    return ap


def default_args(**overrides):
    """Налаштування за замовчуванням; веб-застосунок міняє лише потрібне."""
    args = build_parser().parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    try:                                       # кирилиця у консолі Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                          # noqa: BLE001
        pass

    src = Path(args.input)
    if src.is_dir():
        files = sorted(p for p in src.iterdir()
                       if p.suffix.lower() == ".pdf" or p.suffix.lower() in IMAGE_EXT)
    else:
        files = [src]
    files = [p for p in files if p.is_file()]
    if not files:
        print(f"Не знайдено файлів планів за шляхом: {src}")
        return 1

    tess = find_tesseract(args.tesseract)
    if args.mode != "vector" and not tess:
        print("i  Tesseract не знайдено: скани без текстового шару розпізнати не вийде.\n"
              "   Встановіть https://github.com/UB-Mannheim/tesseract/wiki "
              "або вкажіть --tesseract <шлях>.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for f in files:
        try:
            total += process_file(f, out_dir, args, tess)
        except Exception as exc:               # noqa: BLE001
            print(f"  !! помилка обробки {f.name}: {exc}")

    print(f"\nГотово: {total} файлів у {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
