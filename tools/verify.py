"""Звірка нарізки з відомістю квартир самого аркуша.

Використовує ті самі перевірки, що й веб-застосунок (plan_audit).
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import fitz
from PIL import Image

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                              # noqa: BLE001
    pass

import plan_audit


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="папка з masks.npz, source.png і report.json")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--prefix", default="А")
    ap.add_argument("--tol", type=float, default=plan_audit.AREA_TOLERANCE)
    a = ap.parse_args(argv)

    d = Path(a.dir)
    page = fitz.open(a.pdf)[a.page - 1]
    mask_dpi = json.loads((d / "report.json").read_text(encoding="utf-8"))["info"]["mask_dpi"]
    z = np.load(d / "masks.npz")
    masks = {k: z[k] for k in z.files}
    src = d / "source.png"
    rgb = np.asarray(Image.open(src).convert("RGB")) if src.exists() else None

    problems, rows = plan_audit.audit(page, masks, mask_dpi, rgb, a.prefix, a.tol)
    table = plan_audit.schedule(page, a.prefix)
    print(f"квартир у відомості: {len(table)}; вирізано: {len(masks)}")
    for key, want, got, ratio in sorted(rows, key=lambda r: (len(r[0]), r[0])):
        flag = "" if abs(ratio - 1) <= a.tol else "  <-- ПЕРЕВІРИТИ"
        print(f"  {key:10s} очікується {want:6.2f} м²  "
              f"маска {got:6.2f} м²  x{ratio:4.2f}{flag}")
    if problems:
        print("\nПРОБЛЕМИ:")
        for p in problems:
            print("  !!", p)
    else:
        print("\nрозбіжностей не виявлено")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
