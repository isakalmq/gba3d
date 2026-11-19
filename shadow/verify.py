"""Compare the renderer against mGBA screenshots captured by the bridge."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .protocol import load_capture
from .render import Renderer


def verify_dir(capture_dir: str | Path, save_diffs: bool = True, verbose: bool = True) -> tuple[int, int]:
    """Returns (frames_checked, frames_mismatched)."""
    capture_dir = Path(capture_dir)
    renderer = Renderer(scale=1)
    diff_dir = capture_dir / "diff"
    checked = bad = 0
    states = sorted([*capture_dir.glob("*.gbsh"), *capture_dir.glob("*.gbsh.gz")])
    for state_path in states:
        stem = state_path.name.split(".")[0]
        png = capture_dir / f"{stem}.png"
        if not png.exists():
            continue
        st = load_capture(state_path)
        ours = renderer.render(st).image
        ref = np.asarray(Image.open(png).convert("RGB"))
        mismatch = np.any(ours != ref, axis=-1)
        n = int(mismatch.sum())
        checked += 1
        mode = st.dispcnt & 7
        if n:
            bad += 1
            if save_diffs:
                diff_dir.mkdir(exist_ok=True)
                vis = np.concatenate([ref, ours, np.where(mismatch[..., None], [255, 0, 255], ref // 4).astype(np.uint8)], axis=1)
                Image.fromarray(vis).resize((vis.shape[1] * 2, vis.shape[0] * 2), Image.NEAREST).save(
                    diff_dir / f"{stem}.png")
        if verbose:
            status = "OK  " if n == 0 else "DIFF"
            print(f"{status} {state_path.name}  mode={mode}  dispcnt={st.dispcnt:04x}  mismatched_pixels={n}")
    return checked, bad
