"""Прогін реального конвеєра cut_page для однієї сторінки з діагностикою.

Не прив'язаний до конкретного будинку: приймає input/page/output/dpi.
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import fitz
import cut_apartments as ca


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--page", type=int, default=1, help="номер сторінки, з 1")
    ap.add_argument("-o", "--out", default="tmp/inspect")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--work-dpi", type=int, default=200)
    ap.add_argument("--mask-dpi", type=int, default=150)
    ap.add_argument("--mode", default="auto")
    ap.add_argument("--no-images", action="store_true")
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(a.input)
    page = doc[a.page - 1]
    args = ca.default_args(dpi=a.dpi, work_dpi=a.work_dpi,
                           mask_dpi=a.mask_dpi, mode=a.mode)

    cache = {}
    def img_getter(dpi):
        if dpi not in cache:
            cache[dpi] = ca.page_to_image(page, dpi)
        return cache[dpi]

    t0 = time.time()
    apts, masks, info = ca.cut_page(page, img_getter, args, None, page_no=a.page)
    dt = time.time() - t0

    report = {"file": a.input, "page": a.page, "seconds": round(dt, 1),
              "info": {k: v for k, v in info.items()},
              "apartments": [x.label for x in apts],
              "mask_px": [int(m.sum()) for m in masks]}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if masks:
        np.savez_compressed(out / "masks.npz",
                            **{ca.safe_name(x.label): m for x, m in zip(apts, masks)})
    if masks:
        img_getter(info["mask_dpi"]).save(out / "source.png")
    if not a.no_images and masks:
        base = img_getter(args.dpi)
        for x, m in zip(apts, masks):
            ca.save_apartment(base, m, out / f"{ca.safe_name(x.label)}.png",
                              args.padding, "white", 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
