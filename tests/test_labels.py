# -*- coding: utf-8 -*-
"""Шаблони підписів квартир: що має ловитись, а що ні.

Запуск:  python tests/test_labels.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cut_apartments import BARE_LABEL_RE, OCR_LABEL_RE, parse_label   # noqa: E402

# --- текстовий шар: (рядок, очікуваний підпис або None) -----------------
TEXT_CASES = [
    # з дефісом — як було
    ("А-12.1",      "А-12.1"),
    ("К-3.15",      "К-3.15"),
    ("A-12,1",      "A-12.1"),
    ("А – 12.1",    "А-12.1"),
    # без дефіса — новий формат ("Кімолос-Б")
    ("А3.9",        "А-3.9"),
    ("А3.10",       "А-3.10"),
    ("К 3.1",       "К-3.1"),
    # площі та розміри під шаблон не лізуть
    ("46,46",       None),
    ("51,11 м²",    None),
    ("12,60 м²",    None),
    ("33000",       None),
    ("1 к",         None),
]

# --- рядки OCR: (рядок, очікувані (літера, поверх, номер) або []) -------
OCR_CASES = [
    ("A-12.2 «",              [("A", "12", "2")]),
    ("1 к А3.9 46,46 м²",     [("А", "3", "9")]),
    ("А3.10",                 [("А", "3", "10")]),
    ("К 3.1 2,10 м²",         [("К", "3", "1")]),
    # вісь + площа поруч не повинні злипнутись у підпис
    ("В 13,50 м²",            []),
    ("Г 21,50 м²",            []),
    ("Д 4,31 м²",             []),
    ("46,46 м² 51,11 м²",     []),
]


def main():
    bad = []

    for text, want in TEXT_CASES:
        got = parse_label(text, "А")
        got = got[0] if got else None
        if got != want:
            bad.append(f"parse_label({text!r}) -> {got!r}, очікувалось {want!r}")

    for text, want in OCR_CASES:
        got = [m.groups() for m in OCR_LABEL_RE.finditer(text)]
        if got != want:
            bad.append(f"OCR_LABEL_RE({text!r}) -> {got!r}, очікувалось {want!r}")

    # голий підпис "24.1" лишається окремою гілкою (розрізняється кеглем)
    if not BARE_LABEL_RE.match("24.1"):
        bad.append("BARE_LABEL_RE перестав ловити '24.1'")

    for line in bad:
        print("FAIL:", line)
    total = len(TEXT_CASES) + len(OCR_CASES) + 1
    print(f"\n{total - len(bad)}/{total} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
