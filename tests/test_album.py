# -*- coding: utf-8 -*-
"""Альбом на кілька поверхів: який поверх на аркуші, підпис із кількох
шматків і плашки умовних позначень.

Запуск:  python tests/test_album.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz                                                    # noqa: E402

import numpy as np                                              # noqa: E402

from cut_apartments import (SAME_TINT, find_text_labels,        # noqa: E402
                            legend_swatches, page_floor,
                            same_fill_colour)

ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"ok   {name}")
    else:
        fail += 1
        print(f"FAIL {name}: {got!r}, а мало бути {want!r}")


def sheet(lines, size=(842, 595)):
    """Аркуш із текстом: [(x, y, "текст", кегль)]."""
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    for x, y, text, fs in lines:
        page.insert_text((x, y), text, fontsize=fs)
    return doc, page


class TextOnly:
    """Сторінка, від якої page_floor бере лише текст. Кирилицю у вбудованих
    шрифтах PyMuPDF не намалювати, а перевірити треба саме напис у штампі."""

    def __init__(self, text):
        self._text = text

    def get_text(self, *_a, **_kw):
        return self._text


def fill(x0, y0, x1, y1, colour):
    return {"rect": fitz.Rect(x0, y0, x1, y1), "fill": colour}


# --- який поверх на аркуші -------------------------------------------------
GREY, CREAM, SAGE = (0.9, 0.92, 0.9), (1.0, 1.0, 0.84), (0.82, 0.88, 0.79)

doc, page = sheet([(60 + i * 30, 200, f"VII-14.{i + 1}", 6.7) for i in range(19)])
check("поверх за підписами квартир", page_floor(page), 14)
doc.close()

labels = " ".join(f"VII-14.{i + 1}" for i in range(19))
# У штампі поруч стоїть поверховість будинку — вона не має перебивати підписи.
check("поверховість будинку не збиває",
      page_floor(TextOnly(labels + " Будинок 26 поверхів")), 14)
# Підписів немає — лишається напис.
check("поверх лише з напису", page_floor(TextOnly("План 7 поверху")), 7)
# Порожній аркуш — нічого не вигадуємо.
check("нічого не знайшли — None", page_floor(TextOnly("Розріз 1-1")), None)
# Площі під шаблон лізуть, але більшості не дають.
check("самі площі -> беремо напис",
      page_floor(TextOnly("46,46 51,11 12,60 План 9 поверху")), 9)

# --- підпис, розірваний на кілька шматків ----------------------------------
doc = fitz.open()
page = doc.new_page(width=300, height=200)
# Три шматки одного підпису підряд, як їх інколи віддає текстовий шар PDF.
page.insert_text((50, 100), "VII-", fontsize=7)
page.insert_text((62, 100), "18", fontsize=7)
page.insert_text((70, 100), ".17", fontsize=7)
labels = find_text_labels(page, "А")
check("склеєний підпис знайдено", [(a.floor, a.number) for a in labels],
      [("18", "17")])
doc.close()

# Окремі числа поруч не повинні злипатись у підпис.
doc = fitz.open()
page = doc.new_page(width=300, height=200)
page.insert_text((50, 100), "46,46", fontsize=6)
page.insert_text((80, 100), "51,11", fontsize=6)
check("сусідні площі не стають підписом",
      [(a.floor, a.number) for a in find_text_labels(page, "А")], [])
doc.close()

# --- плашки умовних позначень ----------------------------------------------
legend = [fill(554, 378, 586, 393, GREY),
          fill(554, 396, 586, 410, CREAM),
          fill(554, 413, 586, 428, SAGE)]
check("стовпчик різнокольорових плашок — легенда",
      len(legend_swatches(legend)), 3)

check("три однакові за кольором — не легенда",
      len(legend_swatches([fill(554, 378, 586, 393, GREY),
                           fill(554, 396, 586, 410, GREY),
                           fill(554, 413, 586, 428, GREY)])), 0)

check("двох плашок замало",
      len(legend_swatches([fill(554, 378, 586, 393, GREY),
                           fill(554, 396, 586, 410, CREAM)])), 0)

check("різні за розміром — не легенда",
      len(legend_swatches([fill(554, 378, 586, 393, GREY),
                           fill(554, 396, 620, 410, CREAM),
                           fill(554, 413, 700, 428, SAGE)])), 0)

check("не вишикувані в лінію — не легенда",
      len(legend_swatches([fill(554, 378, 586, 393, GREY),
                           fill(600, 420, 632, 435, CREAM),
                           fill(500, 470, 532, 485, SAGE)])), 0)

# Балкони однакового розміру в стовпчик, але одного кольору — не чіпаємо.
check("однакові балкони в стовпчик уціліли",
      len(legend_swatches([fill(100, 100, 160, 120, CREAM),
                           fill(100, 200, 160, 220, CREAM),
                           fill(100, 300, 160, 320, CREAM)])), 0)

# --- відтінок квартири -----------------------------------------------------
# Балкон залито тим самим кольором, що й квартиру, буква в букву. А сусідні
# типи квартир на пастельній палітрі відрізняються менше, ніж на допуск
# --attach-colour (40) — саме через це балкон однокімнатної діставався студії.
STUDIO = np.array([230.0, 236.0, 230.0])       # студія
ONE_ROOM = np.array([254.0, 255.0, 215.0])     # однокімнатна

check("балкон впізнає свою квартиру",
      same_fill_colour(ONE_ROOM, ONE_ROOM, SAME_TINT), True)
check("сусідній тип квартири — інший тон",
      same_fill_colour(ONE_ROOM, STUDIO, SAME_TINT), False)
check("з допуском --attach-colour ці тони злились би",
      same_fill_colour(ONE_ROOM, STUDIO, 40.0), True)

print(f"\n{ok}/{ok + fail} ok")
sys.exit(1 if fail else 0)
