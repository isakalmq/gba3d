"""Content hashing, replacement packs and dumping of unknown graphics.

Graphics are identified by *content*, not by OAM slot or VRAM address, because
games move both around freely. Each sprite (one OAM entry) and each 8x8 BG tile
gets two hashes:

  pixhash  hash of the decoded palette *indices* (+ size and colour depth)
  palhash  hash of the palette colours it is drawn with

A replacement file named ``<pixhash>-<palhash>.png`` matches only that colour
variant; ``<pixhash>.png`` matches every palette. The specific match wins.

Pack layout::

    mypack/
      pack.json              {"scale": 4}   (optional; default scale for viewing)
      sprites/<hash>.png     replacement for a whole sprite (any size; resized to w*S x h*S)
      tiles/<hash>.png       replacement for one 8x8 BG tile (resized to 8S x 8S)

Transparency comes from the PNG alpha channel (alpha < 128 = transparent).
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image


def _h(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=8).hexdigest()


def sprite_pixhash(indices: np.ndarray, bpp8: bool) -> str:
    """`indices`: (h, w) uint8 of raw tile values (0 = transparent, palette bank not applied)."""
    h, w = indices.shape
    return _h(b"S" + bytes((w, h, int(bpp8))) + np.ascontiguousarray(indices, np.uint8).tobytes())


def tile_pixhash(indices: np.ndarray, bpp8: bool) -> str:
    return _h(b"T" + bytes((int(bpp8),)) + np.ascontiguousarray(indices, np.uint8).tobytes())


def palette_hash(colors: np.ndarray) -> str:
    """`colors`: BGR555 uint16 values. Entry 0 (transparent) is ignored."""
    return _h(np.ascontiguousarray(colors[1:], "<u2").tobytes())


def _load_rgba(path: Path, size: tuple[int, int]) -> np.ndarray:
    """Load a PNG as (h, w, 4) uint8 resized to `size` = (w, h)."""
    img = Image.open(path).convert("RGBA")
    if img.size != size:
        integer_up = size[0] % img.width == 0 and size[1] % img.height == 0 and size[0] >= img.width
        img = img.resize(size, Image.NEAREST if integer_up else Image.LANCZOS)
    return np.asarray(img, np.uint8)


class ReplacementPack:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.scale = 1
        meta = self.root / "pack.json"
        if meta.exists():
            self.scale = int(json.loads(meta.read_text()).get("scale", 1))
        self._files = {
            "sprites": {p.stem: p for p in (self.root / "sprites").glob("*.png")},
            "tiles": {p.stem: p for p in (self.root / "tiles").glob("*.png")},
        }
        self._cache: dict[tuple, np.ndarray | None] = {}

    @property
    def has_sprites(self) -> bool:
        return bool(self._files["sprites"])

    @property
    def has_tiles(self) -> bool:
        return bool(self._files["tiles"])

    def __len__(self) -> int:
        return len(self._files["sprites"]) + len(self._files["tiles"])

    def _lookup(self, kind: str, pixhash: str, palhash: str, size: tuple[int, int]) -> np.ndarray | None:
        key = (kind, pixhash, palhash, size)
        if key not in self._cache:
            files = self._files[kind]
            path = files.get(f"{pixhash}-{palhash}") or files.get(pixhash)
            self._cache[key] = _load_rgba(path, size) if path else None
        return self._cache[key]

    def sprite(self, pixhash: str, palhash: str, w: int, h: int, scale: int) -> np.ndarray | None:
        return self._lookup("sprites", pixhash, palhash, (w * scale, h * scale))

    def tile(self, pixhash: str, palhash: str, scale: int) -> np.ndarray | None:
        return self._lookup("tiles", pixhash, palhash, (8 * scale, 8 * scale))


class Dumper:
    """Writes each never-seen-before sprite / tile once, as a 1x PNG named by its hashes.

    Copy a dumped file into a pack's sprites/ or tiles/ folder, redraw it at any
    resolution, and it will be used as a replacement. Drop the ``-<palhash>``
    part of the name to make it apply to every palette.
    """

    def __init__(self, root: str | Path, tiles: bool = False):
        self.root = Path(root)
        self.tiles = tiles
        (self.root / "sprites").mkdir(parents=True, exist_ok=True)
        if tiles:
            (self.root / "tiles").mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = {p.stem for p in self.root.glob("*/*.png")}
        index = self.root / "index.csv"
        new = not index.exists()
        self._index = index.open("a", newline="")
        self._csv = csv.writer(self._index)
        if new:
            self._csv.writerow(["kind", "pixhash", "palhash", "width", "height", "first_frame"])
        self.count = 0

    def _write(self, kind: str, pixhash: str, palhash: str, rgba: np.ndarray, frame: int) -> None:
        name = f"{pixhash}-{palhash}"
        if name in self._seen:
            return
        self._seen.add(name)
        Image.fromarray(rgba, "RGBA").save(self.root / kind / f"{name}.png")
        self._csv.writerow([kind, pixhash, palhash, rgba.shape[1], rgba.shape[0], frame])
        self._index.flush()
        self.count += 1

    def sprite(self, pixhash: str, palhash: str, rgba: np.ndarray, frame: int) -> None:
        self._write("sprites", pixhash, palhash, rgba, frame)

    def tile(self, pixhash: str, palhash: str, rgba: np.ndarray, frame: int) -> None:
        if self.tiles:
            self._write("tiles", pixhash, palhash, rgba, frame)

    def close(self) -> None:
        self._index.close()
