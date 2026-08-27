# -*- coding: utf-8 -*-
"""Виведення схеми підпису має збігатись із жорстким розбором там, де той працює.

Запасний розбір вмикається лише коли жоден відомий формат не спрацював, тож
перевіряємо головне: якби він запустився на вже робочих кресленнях, він обрав
би ту саму родину підписів, а не якусь іншу.

Запуск:  python tests/test_infer_scheme.py
"""
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fitz                                                        # noqa: E402
from cut_apartments import (choose_label_family, find_text_labels,  # noqa: E402
                            is_apartment_fill, page_drawings,
                            text_label_candidates)

ROOT = Path(__file__).resolve().parents[1]

# (файл, чи має запасний розбір дати ту саму відповідь)
# 6.pdf нумерує кабінети виносками "№ 7" — під шаблон "поверх.номер" вони не
# підпадають за задумом, там працює окрема гілка _labels_from_leaders.
PLANS = [
    ("Эллада-7 12.08.26 12 поверх.pdf", True),
    ("24эт_на сайт.pdf",                True),
    ("6.pdf",                           False),
]


def main():
    bad = []
    for name, should_match in PLANS:
        path = ROOT / name
        if not path.exists():
            print(f"ПРОПУСК: {name} немає у репозиторії")
            continue

        page = fitz.open(path)[0]
        hard = {(a.floor, a.number) for a in find_text_labels(page, "А")}

        n_fills = sum(1 for p in page_drawings(page)
                      if is_apartment_fill(p, 1.2)) or None
        family = choose_label_family(text_label_candidates(page), n_fills,
                                     log=lambda *_: None)
        inferred = {(c.floor, c.number) for c in family}

        # Жорсткий розбір навмисно не чистить за собою: під "поверх.номер"
        # у нього пролазять площі ("39.72"), і відсіює їх уже геометрія.
        # Тому порівнюємо з його справжньою родиною — підписами того поверху,
        # який на кресленні переважає.
        floors = Counter(f for f, _ in hard)
        main_floor = floors.most_common(1)[0][0] if floors else None
        hard_real = {(f, n) for f, n in hard if f == main_floor}

        if should_match:
            ok = bool(hard_real) and hard_real == inferred
            if not ok:
                bad.append(f"{name}: родина {sorted(hard_real)} != виведений {sorted(inferred)}")
            print(f"{name:38s} жорсткий={len(hard):2d} "
                  f"(з них родина {len(hard_real):2d}) виведений={len(inferred):2d} "
                  f"заливок={n_fills}  {'збіг' if ok else 'РОЗБІЖНІСТЬ'}")
        else:
            print(f"{name:38s} жорсткий={len(hard):2d} виведений={len(inferred):2d} "
                  f"(виноски «№ N» — окрема гілка, збігу не очікуємо)")

    for line in bad:
        print("FAIL:", line)
    print("\n" + ("усі збіги на місці" if not bad else f"розбіжностей: {len(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
