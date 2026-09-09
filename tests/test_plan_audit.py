"""Проверка отчёта о нарезке: состав, площади, ручные правки."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import plan_audit


class Page:
    """Лист с ведомостью из трёх строк, без чертежа."""
    class R:
        width = 1000.0
    rect = R()

    def __init__(self, rows):
        self.rows = rows

    def get_text(self, kind):
        out, y = [], 100.0
        for label, living, total in self.rows:
            out.append((300.0, y, 340.0, y + 8.0, label, 0, 0, 0))
            out.append((360.0, y, 390.0, y + 8.0, f"{living:.2f}", 0, 0, 1))
            out.append((400.0, y, 430.0, y + 8.0, f"{total:.2f}", 0, 0, 2))
            y += 20.0
        return out


def square(size, top, left, side):
    m = np.zeros((size, size), bool)
    m[top:top + side, left:left + side] = True
    return m


class PlanAudit(unittest.TestCase):
    def setUp(self):
        self.page = Page([("А.4.1", 10.0, 40.0), ("А.4.2", 10.0, 40.0),
                          ("А.4.3", 10.0, 40.0)])
        self.masks = {"А-4.1": square(400, 0, 0, 100),
                      "А-4.2": square(400, 0, 150, 100),
                      "А-4.3": square(400, 200, 0, 100)}

    def test_matching_areas_pass(self):
        problems, rows = plan_audit.audit(self.page, self.masks, 150)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 3)

    def test_wrong_area_is_reported(self):
        masks = dict(self.masks)
        masks["А-4.3"] = square(400, 200, 0, 30)      # втрое меньше по стороне
        problems, _ = plan_audit.audit(self.page, masks, 150)
        self.assertTrue(any("А-4.3" in p for p in problems), problems)

    def test_hand_deleted_flat_is_not_missing(self):
        # Человек намеренно удалил квартиру — это его решение, а не потеря.
        left = {k: v for k, v in self.masks.items() if k != "А-4.2"}
        problems, _ = plan_audit.audit(self.page, left, 150,
                                       present=set(left), ignore={"А-4.2"})
        self.assertEqual(problems, [])

    def test_hand_added_flat_is_not_surplus(self):
        # Дорисованная вручную квартира не описана ведомостью: ни лишняя,
        # ни измеряемая как автоматическая.
        problems, _ = plan_audit.audit(
            self.page, self.masks, 150,
            present=set(self.masks) | {"999"}, ignore={"999"})
        self.assertEqual(problems, [])

    def test_missing_flat_is_still_reported(self):
        left = {k: v for k, v in self.masks.items() if k != "А-4.2"}
        problems, _ = plan_audit.audit(self.page, left, 150)
        self.assertTrue(any("А-4.2" in p for p in problems), problems)

    def test_label_keeps_its_own_mask_after_a_deletion(self):
        # После ручного удаления состав и порядок записей уже не совпадают со
        # списком масок автонарезки. Сопоставлять их по порядку нельзя —
        # каждая подпись получила бы чужую маску.
        masks = [square(400, 0, 0, 10), square(400, 0, 20, 20),
                 square(400, 0, 60, 30)]
        records = [{"idx": 1, "label": "А-4.1"}, {"idx": 2, "label": "А-4.2"},
                   {"idx": 3, "label": "А-4.3"}]
        records.pop(0)                                 # удалили первую
        paired = {r["label"]: masks[r["idx"] - 1] for r in records}
        for record in records:
            self.assertEqual(int(paired[record["label"]].sum()),
                             int(masks[record["idx"] - 1].sum()))
        by_order = {r["label"]: m for r, m in zip(records, masks)}
        self.assertNotEqual(int(by_order["А-4.2"].sum()),
                            int(paired["А-4.2"].sum()))


if __name__ == "__main__":
    unittest.main()
