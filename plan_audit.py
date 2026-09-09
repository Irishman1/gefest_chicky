"""Перевірка нарізаного поверху за самим кресленням.

Джерело істини — відомість квартир і геометрія аркуша, а не зашитий у код
список будинків. Ті самі перевірки використовують і консольний інструмент,
і веб-застосунок, щоб «перевірено» означало одне й те саме в обох.
"""
import re

import numpy as np

import cut_apartments as ca

NUM = re.compile(r"^\d+[.,]\d\d$")

# Наскільки площа маски може відрізнятись від відомості. У PNG потрапляють
# стіни, тож маска систематично трохи більша; груба ж помилка належності
# змінює площу в рази.
AREA_TOLERANCE = 0.25

# Частка спільної зафарбованої підлоги, вище якої це вже не похибка контуру,
# а дві квартири, що претендують на одне приміщення.
OVERLAP_TOLERANCE = 0.02

# Наскільки може гуляти відступ номера, щоб рядки ще вважались однією колонкою.
COLUMN_TOLERANCE = 6.0


def schedule(page, prefix="А"):
    """Пари «номер квартири -> (житлова, загальна)» з відомості аркуша.

    Відомість буває на три колонки (номер, житлова, загальна) і на дві (номер
    і лише загальна), а номер пишуть і з літерою секції («А.4.12»), і без неї
    («24.12»).

    Рядок збираємо геометрично — числа праворуч від номера на тій самій
    висоті, а не сусідні слова в порядку читання: інакше в рядок таблиці
    потрапляє площа кімнати, що трапилась на тій же висоті на кресленні.

    Без літери «24.12» синтаксично не відрізнити від площі «3.20», тож такі
    рядки приймаються лише коли стоять колонкою й мають спільний номер
    поверху: числа креслення розкидані врозкид і колонки не утворюють.
    """
    words = page.get_text("words")
    nums = [(w, float(w[4].replace(",", "."))) for w in words if NUM.match(w[4])]
    named = re.compile(rf"^{prefix}[.\-]?(\d+)[.\-](\d+)$")
    bare = re.compile(r"^(\d+)[.\-](\d+)$")
    reach = page.rect.width * 0.25

    def scan(pattern):
        rows = []
        for w in words:
            m = pattern.match(w[4])
            if not m:
                continue
            mid = (w[1] + w[3]) / 2.0
            same_row = sorted(((v, value) for v, value in nums
                               if v[1] < mid < v[3]
                               and w[2] <= v[0] <= w[2] + reach),
                              key=lambda t: t[0][0])
            if not same_row:
                continue
            values = [value for _v, value in same_row[:2]]
            rows.append((m.group(1), m.group(2), values, w[0]))
        return rows

    def columns(rows, by_floor):
        """Лише рядки, що стоять колонкою — тобто справді рядки таблиці.

        Підпис на самому кресленні теж схожий на рядок відомості: під номером
        квартири в рамці стоять житлова й загальна площі. Але підписи розкидані
        по аркушу, а відомість — це стовпчик з однаковим відступом.
        """
        # Групуємо за близькістю відступу, а не за округленням: інакше два
        # сусідні значення (1908.0 і 1912.0) можуть потрапити в різні комірки
        # сітки, і перший рядок таблиці випаде з відомості.
        groups, current = [], []
        for row in sorted(rows, key=lambda r: r[3]):
            if current and row[3] - current[-1][3] > COLUMN_TOLERANCE:
                groups.append(current)
                current = []
            current.append(row)
        if current:
            groups.append(current)
        if by_floor:
            split = []
            for group in groups:
                by = {}
                for row in group:
                    by.setdefault(row[0], []).append(row)
                split.extend(by.values())
            groups = split
        solid = [g for g in groups if len(g) >= 3]
        if not solid:
            return []
        if not by_floor:
            return [r for g in solid for r in g]
        weight = {}
        for group in solid:
            weight[group[0][0]] = weight.get(group[0][0], 0) + len(group)
        best = max(weight, key=weight.get)
        return [r for g in solid for r in g if r[0] == best]

    named_rows = scan(named)
    rows = columns(named_rows, by_floor=False) or named_rows
    if len(rows) < 3:
        rows = columns(scan(bare), by_floor=True) or rows

    out = {}
    for floor, num, values, _x in rows:
        key = f"{prefix}-{floor}.{num}"
        if key in out:
            continue                          # двійник із таблиці/креслення
        out[key] = (values[0], values[-1]) if len(values) > 1 else (None, values[0])
    return out


def loggia_bonus(page, masks, mask_dpi):
    """Наскільки маска більша за відомість через лоджії з коефіцієнтом.

    У відомості лоджія враховується зі знижувальним коефіцієнтом («4.52», а в
    підсумок іде «2.26 (k=0.5)»), тоді як на вирізці вона є цілком. Порівнювати
    ці числа як однакові не можна.

    Зменшене число беремо найближче до позначки коефіцієнта (підпис лоджії
    буває й повернутий на 90°, тож порядок читання ненадійний), а повну площу
    рахуємо з самого коефіцієнта — і звіряємо з написаною поруч.
    """
    coeff = re.compile(r"^\(?[kк]\s*=\s*([\d.,]+)\)?$")
    # Другий поширений запис того самого: «4.03*0.5=2.02» одним словом.
    inline = re.compile(r"^([\d.,]+)\s*[*x×]\s*([\d.,]+)\s*=\s*([\d.,]+)$")
    words = page.get_text("words")
    nums = [(w, float(w[4].replace(",", "."))) for w in words if NUM.match(w[4])]
    scale = mask_dpi / 72.0
    bonus = {k: 0.0 for k in masks}
    def credit(word, gain):
        x = int((word[0] + word[2]) / 2.0 * scale)
        y = int((word[1] + word[3]) / 2.0 * scale)
        for key, mask in masks.items():
            if 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and mask[y, x]:
                bonus[key] += gain
                return

    for w in words:
        done = inline.match(w[4])
        if done:
            full = float(done.group(1).replace(",", "."))
            reduced = float(done.group(3).replace(",", "."))
            if 0 < reduced < full:
                credit(w, full - reduced)
            continue
        m = coeff.match(w[4])
        if not m:
            continue
        k = float(m.group(1).replace(",", "."))
        if not 0.05 < k < 1.0:
            continue
        cx, cy = (w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0
        near = sorted(nums, key=lambda t: (t[0][0] + t[0][2] - 2 * cx) ** 2
                                          + (t[0][1] + t[0][3] - 2 * cy) ** 2)[:6]
        # Підпис лоджії — пара «повна площа» / «зменшена (k=…)», і яке з двох
        # чисел ближче до позначки, залежить від верстки. Тому перевіряємо
        # кожного кандидата: справжня пара сама себе підтверджує.
        pair = None
        for _w, value in near:
            full = value / k
            if any(abs(other - full) <= 0.02 * full for _o, other in near
                   if other != value):
                pair = (value, full)
                break
        if pair is None:
            continue
        reduced, full = pair
        credit(w, full - reduced)
    return bonus




def audit(page, masks, mask_dpi, rgb=None, prefix="А", tolerance=AREA_TOLERANCE):
    """Звіряє нарізку з аркушем. Повертає (проблеми, рядки звіту).

    Порожній список проблем не означає «ідеально»: він означає, що ці
    перевірки не знайшли розбіжностей. Відсутність відомості на аркуші — не
    помилка, просто менше даних для звірки.
    """
    problems, rows = [], []
    table = schedule(page, prefix) if page is not None else {}

    if table:
        absent = sorted(set(table) - set(masks))
        surplus = sorted(set(masks) - set(table))
        if absent:
            problems.append("у відомості є, а в нарізці немає: " + ", ".join(absent))
        if surplus:
            problems.append("вирізано поза відомістю: " + ", ".join(surplus))

    area = {k: int(m.sum()) for k, m in masks.items()}
    shared = [k for k in masks if k in table and area[k]]
    if shared:
        bonus = loggia_bonus(page, masks, mask_dpi)
        want = {k: table[k][1] + bonus.get(k, 0.0) for k in shared}
        # Масштаб знімаємо з самого аркуша — так перевірка не залежить від DPI.
        per_m2 = float(np.median([area[k] / want[k] for k in shared]))
        for k in shared:
            ratio = area[k] / want[k] / per_m2
            rows.append((k, want[k], area[k] / per_m2, ratio))
            if abs(ratio - 1.0) > tolerance:
                problems.append(f"{k}: площа вирізки відрізняється від відомості "
                                f"у {ratio:.2f} раза")

    if rgb is not None and len(masks) > 1:
        # Спільну стіну дозволено показати в обох PNG, тож перетин рахуємо по
        # зафарбованій підлозі: внутрішні площі перетинатись не мають.
        std = ca.default_args()
        floor = ca.colored_fill_mask(rgb, ca.adaptive_saturation(rgb, std.sat), std.dark)
        inside = {k: m & floor for k, m in masks.items()}
        keys = sorted(masks)
        for i, ka in enumerate(keys):
            for kb in keys[i + 1:]:
                both = int((inside[ka] & inside[kb]).sum())
                smaller = min(int(inside[ka].sum()), int(inside[kb].sum()))
                if smaller and both / smaller > OVERLAP_TOLERANCE:
                    problems.append(f"внутрішні площі {ka} і {kb} перетинаються "
                                    f"на {both / smaller * 100:.0f}% меншої")
    return problems, rows
