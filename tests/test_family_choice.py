# -*- coding: utf-8 -*-
"""Поведінка відбору родини підписів на штучних даних.

Головне, що тут перевіряється, — що відбір не вгадує навмання: коли два
претенденти рівноцінні, він має чесно повернути порожньо, а не кинути монетку.

Запуск:  python tests/test_family_choice.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cut_apartments import LabelCandidate, choose_label_family   # noqa: E402

quiet = lambda *_a, **_k: None                                   # noqa: E731


def cand(letter, floor, num, size=12.0, x=0.0, y=0.0):
    return LabelCandidate(letter, floor, str(num), (x, y), size)


def family(cands, n_fills=None):
    return sorted(int(c.number) for c in choose_label_family(cands, n_fills, quiet))


def main():
    bad = []

    def check(name, got, want):
        if got != want:
            bad.append(f"{name}: {got} != {want}")
        print(f"{'ok  ' if got == want else 'FAIL'} {name}")

    # 1. підписи серед шуму: площі дрібніші й ідуть врозсип
    labels = [cand("А", "3", i, size=12.0) for i in range(1, 11)]
    areas = [cand("", "46", 46, size=8.0), cand("", "51", 11, size=8.0),
             cand("", "12", 60, size=8.0), cand("", "20", 57, size=8.0),
             cand("", "15", 67, size=8.0)]
    check("родина відокремлюється від площ", family(labels + areas, 10),
          list(range(1, 11)))

    # 2. кількість заливок підказує правильну родину з двох схожих
    a = [cand("А", "3", i, size=12.0) for i in range(1, 11)]
    b = [cand("Б", "9", i, size=12.0) for i in range(1, 5)]
    check("заливки обирають більшу родину", family(a + b, 10), list(range(1, 11)))

    # 3. дірки в ряду знижують оцінку
    solid = [cand("А", "3", i, size=12.0) for i in range(1, 9)]
    holey = [cand("Б", "7", i, size=12.0) for i in (1, 14, 27, 39, 55, 61, 78, 92)]
    check("суцільний ряд виграє в дірявого", family(solid + holey),
          list(range(1, 9)))

    # 4. дві рівноцінні родини — чесна відмова
    x = [cand("А", "3", i, size=12.0) for i in range(1, 9)]
    y = [cand("Б", "4", i, size=12.0) for i in range(1, 9)]
    check("рівноцінні родини — відмова", family(x + y), [])

    # 5. порожній вхід не падає
    check("порожній вхід", family([]), [])

    # 6. одинокий кандидат — не родина
    check("один кандидат — не родина", family([cand("А", "3", 1)]), [])

    for line in bad:
        print("  ", line)
    print(f"\n{6 - len(bad)}/6 ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
