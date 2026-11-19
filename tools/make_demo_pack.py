"""Build a demo replacement pack from a dump directory.

Every dumped sprite/tile is upscaled 4x with Scale2x (applied twice), and
sprites also get a dark outline, so replaced graphics are easy to spot.
It stands in for real hand-drawn art while testing the pipeline.

    python tools/make_demo_pack.py DUMP_DIR OUT_PACK_DIR [--any-palette]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def scale2x(img: np.ndarray) -> np.ndarray:
    """Scale2x / EPX on an (h, w, 4) RGBA image; compares whole pixels."""
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    key = p.astype(np.uint32)
    key = (key[..., 0] << 24) | (key[..., 1] << 16) | (key[..., 2] << 8) | key[..., 3]
    B, D, E, F, Hh = key[:-2, 1:-1], key[1:-1, :-2], key[1:-1, 1:-1], key[1:-1, 2:], key[2:, 1:-1]
    src, left, right = p[1:-1, 1:-1], p[1:-1, :-2], p[1:-1, 2:]
    cond = (B != Hh) & (D != F)
    e0 = np.where((cond & (D == B))[..., None], left, src)
    e1 = np.where((cond & (B == F))[..., None], right, src)
    e2 = np.where((cond & (D == Hh))[..., None], left, src)
    e3 = np.where((cond & (Hh == F))[..., None], right, src)
    h, w = E.shape
    out = np.empty((h * 2, w * 2, 4), img.dtype)
    out[0::2, 0::2], out[0::2, 1::2], out[1::2, 0::2], out[1::2, 1::2] = e0, e1, e2, e3
    return out


def outline(img: np.ndarray, color=(16, 16, 24)) -> np.ndarray:
    a = img[..., 3] >= 128
    grown = a.copy()
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        grown |= np.roll(np.roll(a, dy, 0), dx, 1)
    out = img.copy()
    edge = grown & ~a
    out[edge] = (*color, 255)
    # Pull the outline inside the box so it is not clipped at the sprite edges.
    border = np.zeros_like(a)
    border[[0, -1], :] = border[:, [0, -1]] = True
    out[border & a] = (*color, 255)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump_dir")
    ap.add_argument("pack_dir")
    ap.add_argument("--any-palette", action="store_true",
                    help="name files by pixel hash only, so they match every palette")
    args = ap.parse_args()
    dump, pack = Path(args.dump_dir), Path(args.pack_dir)
    count = 0
    for kind in ("sprites", "tiles"):
        src_dir = dump / kind
        if not src_dir.is_dir():
            continue
        (pack / kind).mkdir(parents=True, exist_ok=True)
        for f in sorted(src_dir.glob("*.png")):
            img = np.asarray(Image.open(f).convert("RGBA"))
            hi = scale2x(scale2x(img))
            if kind == "sprites":
                hi = outline(hi)
            name = f.stem.split("-")[0] if args.any_palette else f.stem
            Image.fromarray(hi, "RGBA").save(pack / kind / f"{name}.png")
            count += 1
    (pack / "pack.json").write_text(json.dumps({"scale": 4}, indent=2) + "\n")
    print(f"wrote {count} replacements to {pack}")


if __name__ == "__main__":
    main()
