"""Наклад масок квартир на вихідний аркуш — швидка візуальна перевірка."""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from PIL import Image

PALETTE = [(230,60,60),(60,140,230),(60,190,90),(240,160,40),(170,80,220),
           (30,200,200),(230,100,180),(140,140,40),(90,90,220),(200,60,120)]

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("-o","--out",default=None)
    ap.add_argument("--scale",type=float,default=0.35)
    a = ap.parse_args(argv)
    d = Path(a.dir)
    src = np.asarray(Image.open(d/"source.png").convert("RGB")).astype(np.float32)
    z = np.load(d/"masks.npz")
    over = src.copy()
    for i,k in enumerate(z.files):
        m = z[k]
        if m.shape != src.shape[:2]:
            m = np.asarray(Image.fromarray(m).resize((src.shape[1],src.shape[0]),Image.NEAREST))
        c = np.array(PALETTE[i%len(PALETTE)],np.float32)
        over[m] = over[m]*0.55 + c*0.45
    img = Image.fromarray(over.astype(np.uint8))
    img = img.resize((int(img.width*a.scale),int(img.height*a.scale)),Image.LANCZOS)
    out = Path(a.out or d/"overlay.png")
    img.save(out); print(out, img.size)

if __name__=="__main__": sys.exit(main())
