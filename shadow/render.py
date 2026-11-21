"""Re-renders a GBA frame from raw display state, optionally swapping in replacement art.

How it works
------------
Every layer (BG0-3, OBJ) is evaluated at a list of sample points and then
composited per point (priorities, windows, blending), following mGBA's
software renderer rules.

1. A 1x pass samples every GBA pixel, using only original graphics. This pass
   matches mGBA pixel for pixel (checked by `python -m shadow verify`), and it
   also records which pixels are covered by graphics that have a replacement.
2. The 1x image is upscaled to the output scale S.
3. Only the pixels touched by replacements are re-evaluated on the S-times finer
   grid, with replacement art sampled at full resolution and original art
   sampled exactly as in the 1x pass. So non-replaced content looks identical
   everywhere and the expensive part scales with the amount of replaced
   content, not the screen size.

Limitations (deliberate for this prototype):
  * state is sampled once per frame, so mid-frame (HBlank/raster) effects are lost
  * mosaic is ignored
  * the per-scanline sprite pixel budget is not emulated
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .pack import Dumper, ReplacementPack, palette_hash, sprite_pixhash, tile_pixhash
from .state import SCREEN_H as H
from .state import SCREEN_W as W
from .state import GBAState

# (width, height) indexed by [shape][size]
OBJ_SIZES = (
    ((8, 8), (16, 16), (32, 32), (64, 64)),
    ((16, 8), (32, 8), (32, 16), (64, 32)),
    ((8, 16), (8, 32), (16, 32), (32, 64)),
)

LAYER_OBJ, LAYER_BACKDROP = 4, 5
KEY_NONE = 255


def bgr555_to_rgb8(c: np.ndarray) -> np.ndarray:
    """BGR555 -> (..., 3) int32 RGB888 the way mGBA expands it: (c << 3) | (c >> 2)."""
    c = np.asarray(c).astype(np.int32)
    rgb = np.stack([c & 31, (c >> 5) & 31, (c >> 10) & 31], axis=-1)
    return (rgb << 3) | (rgb >> 2)


def _decode_4bpp(raw: np.ndarray) -> np.ndarray:
    b = raw.reshape(-1, 8, 4)
    out = np.empty((b.shape[0], 8, 8), np.uint8)
    out[:, :, 0::2] = b & 15
    out[:, :, 1::2] = b >> 4
    return out


def _decode_8bpp_units(raw: np.ndarray, wrap: bool) -> np.ndarray:
    """8bpp tiles addressed in 32-byte units (how tile numbers count). Unit n = raw[n*32 : n*32+64]."""
    shifted = np.roll(raw, -32) if wrap else np.concatenate([raw[32:], np.zeros(32, np.uint8)])
    out = np.empty((len(raw) // 32, 8, 8), np.uint8)
    out[0::2] = raw.reshape(-1, 8, 8)
    out[1::2] = shifted.reshape(-1, 8, 8)
    return out


def _window_range(coords: np.ndarray, start: int, end: int, limit: int) -> np.ndarray:
    # Same normalisation mGBA applies when WINnH / WINnV are written.
    if start > limit and start > end:
        start = 0
    if end > limit:
        end = limit
        if start > limit:
            start = limit
    if end >= start:
        return (coords >= start) & (coords < end)
    return (coords >= start) | (coords < end)


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx or dy:
                out |= np.roll(np.roll(mask, dy, 0), dx, 1)
    return out


class _Points:
    """Sample points in output space, sorted row-major. X/Y are hi-res, gx/gy the GBA pixel."""

    def __init__(self, X: np.ndarray, Y: np.ndarray, S: int):
        self.X, self.Y, self.S = X, Y, S
        self.gx, self.gy = X // S, Y // S
        self.n = len(X)

    @classmethod
    def grid(cls) -> "_Points":
        ys, xs = np.mgrid[0:H, 0:W]
        return cls(xs.ravel().astype(np.int64), ys.ravel().astype(np.int64), 1)


@dataclass
class SpriteInfo:
    index: int          # OAM slot
    x: int              # on-screen bounding box, GBA pixels (may be partly off-screen)
    y: int
    width: int
    height: int
    affine: bool
    priority: int
    mode: int           # 0 normal, 1 semi-transparent, 2 OBJ window
    pixhash: str
    palhash: str
    replaced: bool


@dataclass
class _Sprite:
    info: SpriteInfo
    w: int
    h: int
    hflip: bool
    vflip: bool
    matrix: tuple[int, int, int, int] | None
    src1: np.ndarray                 # (h, w, 4) original RGBA
    srch: np.ndarray | None          # (h*S, w*S, 4) replacement RGBA


@dataclass
class _BG:
    bit: int
    kind: str                        # "text" | "affine" | "bitmap"
    prio: int
    cnt: int
    table: np.ndarray | None = None  # tile key -> atlas index (-1 = none)
    atlas: np.ndarray | None = None  # (N, 8S, 8S, 4)


def _rgba(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    out = np.zeros((H, W, 4), np.uint8)
    out[..., :3] = rgb.reshape(H, W, 3)
    out[..., 3] = np.where(mask, 255, 0).reshape(H, W)
    return out


@dataclass
class SplitResult:
    behind: np.ndarray                  # (H, W, 3)
    front: np.ndarray | None = None     # (H, W, 4) RGBA
    hud: np.ndarray | None = None       # (H, W, 4) RGBA


@dataclass
class RenderResult:
    image: np.ndarray                                   # (H*S, W*S, 3) uint8
    sprites: list[SpriteInfo] = field(default_factory=list)
    tiles_replaced: int = 0
    hires_pixels: int = 0                               # GBA pixels re-rendered at hi-res


class Renderer:
    def __init__(self, scale: int = 1, pack: ReplacementPack | None = None, dumper: Dumper | None = None):
        self.scale = scale
        self.pack = pack
        self.dumper = dumper
        self.replace = True     # toggle replacements without unloading the pack
        self._grid = _Points.grid()

    # ------------------------------------------------------------------ setup
    def _prepare(self, st: GBAState) -> None:
        self.st = st
        self.pal = bgr555_to_rgb8(st.palette16)                      # (512, 3): 0-255 BG, 256-511 OBJ
        bg = st.vram[:0x10000]
        self.bg4 = _decode_4bpp(bg)                                  # (2048, 8, 8)
        self.bg8 = _decode_8bpp_units(bg, wrap=False)                # (2048, 8, 8)
        obj = st.vram[0x10000:0x18000]
        self.obj4 = _decode_4bpp(obj)                                # (1024, 8, 8)
        self.obj8 = _decode_8bpp_units(obj, wrap=True)               # (1024, 8, 8)

    def _tiles_wanted(self) -> bool:
        return bool(self.pack and self.replace and self.pack.has_tiles) or bool(self.dumper and self.dumper.tiles)

    # -------------------------------------------------------------- BG tiles
    def _build_tile_table(self, bg: _BG, keys: np.ndarray, valid: np.ndarray, bpp8: bool) -> None:
        """Hash every visible (tile, palette bank), dump new ones, look up replacements."""
        S, st = self.scale, self.st
        use_pack = bool(self.pack and self.replace and self.pack.has_tiles)
        table = np.full(2048 * 16, -1, np.int32)
        atlas = []
        for k in np.unique(keys[valid]).tolist():
            unit, bank = divmod(k, 16)
            idx = (self.bg8 if bpp8 else self.bg4)[unit]
            pix = tile_pixhash(idx, bpp8)
            pal = palette_hash(st.palette16[:256] if bpp8 else st.palette16[bank * 16:bank * 16 + 16])
            if self.dumper is not None and self.dumper.tiles:
                full = idx.astype(np.int64)
                full = full if bpp8 else np.where(full != 0, full + bank * 16, 0)
                rgba = np.dstack([self.pal[full], np.where(idx != 0, 255, 0)]).astype(np.uint8)
                self.dumper.tile(pix, pal, rgba, st.frame)
            if use_pack:
                img = self.pack.tile(pix, pal, S)
                if img is not None:
                    table[k] = len(atlas)
                    atlas.append(img)
        if atlas:
            bg.table, bg.atlas = table, np.stack(atlas)

    def _text_coords(self, bg: _BG, X: np.ndarray, Y: np.ndarray, S: int):
        """Per point: tile unit, palette bank, in-tile coords (1/S px, flips applied), valid, bpp8, flips."""
        st, cnt, i = self.st, bg.cnt, bg.bit
        cb, bpp8, sb, size = (cnt >> 2) & 3, (cnt >> 7) & 1, (cnt >> 8) & 31, cnt >> 14
        wm = 512 if size & 1 else 256
        hm = 512 if size & 2 else 256
        mxh = (X + (st.reg(0x10 + 4 * i) & 511) * S) % (wm * S)
        myh = (Y + (st.reg(0x12 + 4 * i) & 511) * S) % (hm * S)
        tx, ty = mxh // S >> 3, myh // S >> 3
        addr = sb * 0x400 + ((tx >> 5) + (ty >> 5) * (wm // 256)) * 0x400 + (ty & 31) * 32 + (tx & 31)
        e = st.vram16[addr & 0x7FFF].astype(np.int64)
        hf, vf = ((e >> 10) & 1).astype(bool), ((e >> 11) & 1).astype(bool)
        sx, sy = mxh % (8 * S), myh % (8 * S)
        sx = np.where(hf, 8 * S - 1 - sx, sx)
        sy = np.where(vf, 8 * S - 1 - sy, sy)
        if bpp8:
            unit, bank = cb * 512 + (e & 0x3FF) * 2, np.zeros_like(e)
        else:
            unit, bank = cb * 512 + (e & 0x3FF), e >> 12
        return unit, bank, sx, sy, unit < 2048, bool(bpp8), (hf, vf)

    def _affine_texcoords(self, i: int, X: np.ndarray, Y: np.ndarray, S: int):
        """Texture coordinates in 1/S texels for affine BG `i` (2 or 3). Exact integer math at S=1."""
        st = self.st
        base = 0x20 + (i - 2) * 0x10
        pa, pb, pc, pd = (st.reg_s16(base + k) for k in (0, 2, 4, 6))
        x0, y0 = st.reg_s28(base + 8), st.reg_s28(base + 12)
        return (x0 * S + pa * X + pb * Y) >> 8, (y0 * S + pc * X + pd * Y) >> 8

    def _affine_coords(self, bg: _BG, X: np.ndarray, Y: np.ndarray, S: int):
        cnt = bg.cnt
        cb, sb, wrap = (cnt >> 2) & 3, (cnt >> 8) & 31, (cnt >> 13) & 1
        size = 128 << (cnt >> 14)
        uh, vh = self._affine_texcoords(bg.bit, X, Y, S)
        if wrap:
            uh, vh = uh % (size * S), vh % (size * S)
            inside = np.ones(uh.shape, bool)
        else:
            inside = (uh >= 0) & (uh < size * S) & (vh >= 0) & (vh < size * S)
            uh, vh = np.where(inside, uh, 0), np.where(inside, vh, 0)
        tu, tv = uh // S, vh // S
        tile = self.st.vram[(sb * 0x800 + (tv >> 3) * (size >> 3) + (tu >> 3)) & 0xFFFF].astype(np.int64)
        unit = cb * 512 + tile * 2
        return unit, np.zeros_like(unit), uh % (8 * S), vh % (8 * S), inside, True, None

    def _sample_bg(self, bg: _BG, pts: _Points, hires: bool):
        """Returns color (n,3), opaque (n,), touched (n,) or None.

        The 1x pass (hires=False, pts = full grid) samples original graphics and
        caches the results on `bg`. The hi-res pass gathers those and only
        computes replacement texels on the fine grid.
        """
        if not hires:
            if bg.kind == "bitmap":
                bg.c_color, bg.c_opaque = self._sample_bitmap(pts)
                return bg.c_color, bg.c_opaque, None
            coords = self._text_coords if bg.kind == "text" else self._affine_coords
            unit, bank, sx, sy, valid, bpp8, flips = coords(bg, pts.gx, pts.gy, 1)
            idx = (self.bg8 if bpp8 else self.bg4)[unit & 2047, sy, sx]
            cidx = idx.astype(np.int64) if bpp8 else bank * 16 + idx
            bg.c_color, bg.c_opaque = self.pal[cidx], valid & (idx != 0)
            keys = (unit * 16 + bank) & 0x7FFF
            if self._tiles_wanted():
                self._build_tile_table(bg, keys, valid, bpp8)
            touched = None
            if bg.table is not None:
                touched = valid & (bg.table[keys] >= 0)
                if flips is not None:
                    hf, vf = flips
                    bg.c_keys, bg.c_valid, bg.c_sx, bg.c_sy, bg.c_hf, bg.c_vf = keys, valid, sx, sy, hf, vf
            return bg.c_color, bg.c_opaque, touched

        f = pts.gy * W + pts.gx
        color, opaque = bg.c_color[f], bg.c_opaque[f]
        if bg.table is None:
            return color, opaque, None
        S = pts.S
        if bg.kind == "text":
            # Text BGs scroll by whole pixels, so a hi-res point lies in the same
            # tile as its GBA pixel; only the position inside the tile is finer.
            rid = np.where(bg.c_valid[f], bg.table[bg.c_keys[f]], -1)
            m = rid >= 0
            fm = f[m]
            subx, suby = pts.X[m] % S, pts.Y[m] % S
            sx = bg.c_sx[fm] * S + np.where(bg.c_hf[fm], S - 1 - subx, subx)
            sy = bg.c_sy[fm] * S + np.where(bg.c_vf[fm], S - 1 - suby, suby)
            texel = bg.atlas[rid[m], sy, sx]
        else:
            unit, bank, sx, sy, valid, _, _ = self._affine_coords(bg, pts.X, pts.Y, S)
            rid = np.where(valid, bg.table[(unit * 16 + bank) & 0x7FFF], -1)
            m = rid >= 0
            texel = bg.atlas[rid[m], sy[m], sx[m]]
        color[m] = texel[:, :3]
        opaque[m] = texel[:, 3] >= 128
        return color, opaque, None

    def _sample_bitmap(self, pts: _Points):
        st, mode = self.st, self.st.mode
        tu, tv = self._affine_texcoords(2, pts.gx, pts.gy, 1)
        bw, bh = (160, 128) if mode == 5 else (240, 160)
        inside = (tu >= 0) & (tu < bw) & (tv >= 0) & (tv < bh)
        tu, tv = np.where(inside, tu, 0), np.where(inside, tv, 0)
        page = 0xA000 if (mode != 3 and st.dispcnt & 0x10) else 0
        if mode == 4:
            idx = st.vram[page + tv * 240 + tu]
            return self.pal[idx], inside & (idx != 0)
        return bgr555_to_rgb8(st.vram16[page // 2 + tv * bw + tu]), inside

    # -------------------------------------------------------------- sprites
    def _sprite_indices(self, a0: int, a2: int, w: int, h: int, mapping_1d: bool):
        bpp8 = bool((a0 >> 13) & 1)
        tile = a2 & 1023
        tw, th = w // 8, h // 8
        step = 2 if bpp8 else 1
        if bpp8 and not mapping_1d:
            tile &= ~1
        ty, tx = np.arange(th)[:, None], np.arange(tw)[None, :]
        if mapping_1d:
            units = (tile + ty * tw * step + tx * step) & 1023
        else:  # 2D: 32 tiles per row, X wraps inside the row
            units = ((tile & ~31) + ty * 32 + ((tile + tx * step) & 31)) & 1023
        tiles = (self.obj8 if bpp8 else self.obj4)[units]           # (th, tw, 8, 8)
        return tiles.transpose(0, 2, 1, 3).reshape(h, w), bpp8

    def _plan_sprites(self, hide_oam=frozenset()) -> list[_Sprite]:
        """Decode, hash and look up replacements for every visible OAM entry (in OAM order)."""
        st, S = self.st, self.scale
        dispcnt = st.dispcnt
        if not dispcnt & 0x1000:
            return []
        objwin_enabled = bool(dispcnt & 0x8000)
        mapping_1d = bool(dispcnt & 0x40)
        bitmap_mode = st.mode >= 3
        oam = st.oam16.astype(np.int64)
        use_pack = bool(self.pack and self.replace and self.pack.has_sprites)
        out = []
        for i in range(128):
            if i in hide_oam:
                continue
            a0, a1, a2 = int(oam[i * 4]), int(oam[i * 4 + 1]), int(oam[i * 4 + 2])
            affine, flag9 = (a0 >> 8) & 1, (a0 >> 9) & 1
            if not affine and flag9:
                continue                                  # disabled
            mode, shape = (a0 >> 10) & 3, a0 >> 14
            if mode == 3 or shape == 3:
                continue                                  # prohibited values
            if mode == 2 and not objwin_enabled:
                continue
            w, h = OBJ_SIZES[shape][a1 >> 14]
            bw, bh = (2 * w, 2 * h) if (affine and flag9) else (w, h)
            x, y = a1 & 511, a0 & 255
            if x >= W:
                x -= 512
            if y >= H:
                y -= 256
            if x >= W or y >= H or x + bw <= 0 or y + bh <= 0:
                continue
            if bitmap_mode and (a2 & 1023) < 512:
                continue
            prio, bank = (a2 >> 10) & 3, a2 >> 12

            idx, bpp8 = self._sprite_indices(a0, a2, w, h, mapping_1d)
            idx64 = idx.astype(np.int64)
            cidx = 256 + (idx64 if bpp8 else np.where(idx64 != 0, bank * 16 + idx64, 0))
            src1 = np.dstack([self.pal[cidx], np.where(idx != 0, 255, 0)]).astype(np.uint8)
            pal_colors = st.palette16[256:512] if bpp8 else st.palette16[256 + bank * 16:256 + bank * 16 + 16]
            pixhash, palhash = sprite_pixhash(idx, bpp8), palette_hash(pal_colors)
            if self.dumper is not None and mode != 2:
                self.dumper.sprite(pixhash, palhash, src1, st.frame)
            srch = self.pack.sprite(pixhash, palhash, w, h, S) if use_pack else None

            matrix = None
            if affine:
                set_ = (a1 >> 9) & 31
                matrix = tuple(int(np.int16(st.oam16[set_ * 16 + 3 + 4 * k])) for k in range(4))
            info = SpriteInfo(i, x, y, bw, bh, bool(affine), prio, mode, pixhash, palhash, srch is not None)
            out.append(_Sprite(info, w, h, bool((a1 >> 12) & 1), bool((a1 >> 13) & 1), matrix, src1, srch))
        return out

    def _sample_sprites(self, sprites: list[_Sprite], pts: _Points, hires: bool):
        n, S = pts.n, pts.S
        color = np.zeros((n, 3), np.int32)
        prio_l = np.full(n, 4, np.int8)
        idx_l = np.full(n, 128, np.int16)           # OAM slot that drew each pixel
        written = np.zeros(n, bool)
        semi = np.zeros(n, bool)
        objwin = np.zeros(n, bool)
        touched = None if hires else np.zeros(n, bool)
        for spr in sprites:
            info = spr.info
            lo, hi = np.searchsorted(pts.gy, [info.y, info.y + info.height])
            if lo >= hi:
                continue
            gx = pts.gx[lo:hi]
            sel = lo + np.flatnonzero((gx >= info.x) & (gx < info.x + info.width))
            if not len(sel):
                continue
            if touched is not None and spr.srch is not None:
                touched[sel] = True

            if hires and spr.srch is not None:
                src, k = spr.srch, S
                lx, ly = pts.X[sel] - info.x * S, pts.Y[sel] - info.y * S
            else:
                src, k = spr.src1, 1
                lx, ly = pts.gx[sel] - info.x, pts.gy[sel] - info.y
            w, h = spr.w * k, spr.h * k
            if spr.matrix:
                pa, pb, pc, pd = spr.matrix
                dx, dy = lx - (info.width * k) // 2, ly - (info.height * k) // 2
                tx = ((pa * dx + pb * dy) >> 8) + w // 2
                ty = ((pc * dx + pd * dy) >> 8) + h // 2
                cover = (tx >= 0) & (tx < w) & (ty >= 0) & (ty < h)
                texel = src[np.where(cover, ty, 0), np.where(cover, tx, 0)]
            else:
                tx = w - 1 - lx if spr.hflip else lx
                ty = h - 1 - ly if spr.vflip else ly
                cover = np.ones(len(sel), bool)
                texel = src[ty, tx]
            op = cover & (texel[:, 3] >= 128)
            if info.mode == 2:
                objwin[sel] |= op
                continue
            # mGBA/hardware rule: a pixel is taken if its priority beats what is there;
            # equal priority keeps the lower OAM index. A *transparent* pixel of a
            # better-priority sprite still lowers the priority of what is underneath.
            upd = cover & (info.priority < prio_l[sel])
            take = upd & op
            reprio = upd & ~op & written[sel]
            color[sel[take]] = texel[take, :3]
            written[sel[take]] = True
            idx_l[sel[take]] = info.index
            changed = sel[take | reprio]
            prio_l[changed] = info.priority
            semi[changed] = info.mode == 1
        self._obj_index = idx_l
        return color, written, prio_l, semi, objwin, touched

    # ----------------------------------------------------------- compositing
    def _window_mask(self, pts: _Points, objwin: np.ndarray | None) -> np.ndarray:
        st = self.st
        dispcnt = st.dispcnt
        if not dispcnt & 0xE000:
            return np.full(pts.n, 0x3F, np.uint8)
        winin, winout = st.reg(0x48), st.reg(0x4A)
        mask = np.full(pts.n, winout & 0x3F, np.uint8)
        if dispcnt & 0x8000 and objwin is not None:
            mask[objwin] = (winout >> 8) & 0x3F
        for n in (1, 0):  # WIN0 has priority over WIN1
            if dispcnt & (0x2000 << n):
                hreg, vreg = st.reg(0x40 + 2 * n), st.reg(0x44 + 2 * n)
                inside = _window_range(pts.gx, hreg >> 8, hreg & 0xFF, W) & \
                    _window_range(pts.gy, vreg >> 8, vreg & 0xFF, H)
                mask[inside] = (winin >> (8 * n)) & 0x3F
        return mask

    def _evaluate(self, bgs: list[_BG], sprites: list[_Sprite], pts: _Points, hires: bool):
        """Composite all layers at `pts`. Returns (rgb (n,3) uint8, touched (n,) bool or None)."""
        st, n = self.st, pts.n
        touched = None
        layers = []  # (bit, color, visible-before-window, prio)
        for bg in bgs:
            color, opaque, t = self._sample_bg(bg, pts, hires)
            layers.append((bg.bit, color, opaque, bg.prio))
            if t is not None:
                touched = t if touched is None else touched | t
        semi = objwin = None
        if st.dispcnt & 0x1000:
            color, opaque, prio, semi, objwin, t = self._sample_sprites(sprites, pts, hires)
            layers.append((LAYER_OBJ, color, opaque, prio))
            if t is not None:
                touched = t if touched is None else touched | t

        win = self._window_mask(pts, objwin)

        # Sort keys: lower = in front. OBJ beats a BG of equal priority; lower BG index beats higher.
        keys = np.empty((len(layers) + 1, n), np.uint8)
        keys[0] = 128                                                  # backdrop
        ids = [LAYER_BACKDROP]
        for li, (bit, _, opaque, prio) in enumerate(layers, start=1):
            k = np.asarray(prio, np.int32) * 16 + (0 if bit == LAYER_OBJ else 4 + bit)
            visible = opaque & ((win >> bit) & 1).astype(bool)
            keys[li] = np.where(visible, k, KEY_NONE)
            ids.append(bit)
        cols = np.arange(n)
        top = keys.argmin(0)
        top_key = keys[top, cols].copy()
        keys[top, cols] = KEY_NONE
        second = keys.argmin(0)
        second_valid = keys[second, cols] != KEY_NONE

        stack = np.empty((len(layers) + 1, n, 3), np.int32)
        stack[0] = self.pal[0]
        for li, (_, color, _, _) in enumerate(layers, start=1):
            stack[li] = color
        cols = np.arange(n)
        top_c, sec_c = stack[top, cols], stack[second, cols]
        ids_arr = np.array(ids)
        top_bit, sec_bit = ids_arr[top], ids_arr[second]
        # For splitting a frame into layers: which layer won each point, its
        # priority (backdrop = 8) and, for sprites, the OAM slot.
        self._top = (top_bit, top_key // 16,
                     np.where(top_bit == LAYER_OBJ, self._obj_index, 128) if semi is not None else np.full(n, 128))

        bldcnt = st.reg(0x50)
        target1, target2, effect = bldcnt & 0x3F, (bldcnt >> 8) & 0x3F, (bldcnt >> 6) & 3
        bldalpha = st.reg(0x52)
        eva, evb = min(16, bldalpha & 31), min(16, (bldalpha >> 8) & 31)
        evy = min(16, st.reg(0x54) & 31)

        eff = ((win >> 5) & 1).astype(bool)
        is_t1 = ((target1 >> top_bit) & 1).astype(bool)
        is_t2 = second_valid & ((target2 >> sec_bit) & 1).astype(bool)
        semi_top = (top_bit == LAYER_OBJ) & semi if semi is not None else np.zeros(n, bool)
        alpha = is_t2 & (semi_top | ((effect == 1) & is_t1 & eff))
        out = top_c
        if alpha.any():
            out[alpha] = np.minimum(255, (top_c[alpha] * eva + sec_c[alpha] * evb) >> 4)
        if effect in (2, 3):
            # Semi-transparent OBJs count as 1st target for brighten/darken only
            # when some enabled layer is a 2nd target (mGBA's "reblend" path).
            enabled_bits = 0x20 | sum(1 << bit for bit, *_ in layers if bit != LAYER_OBJ)
            semi_t1 = semi_top & bool(target2 & enabled_bits)
            bright = ~alpha & eff & (is_t1 | semi_t1)
            c = out[bright]
            if effect == 2:
                out[bright] = c + (((255 - c) * evy) >> 4)
            else:
                # mGBA darkens G and B on their shifted-in-place values, which
                # effectively rounds the subtraction up for those two channels.
                out[bright] = c - ((c * evy + np.array([0, 15, 15])) >> 4)
        return out.astype(np.uint8), touched

    # ------------------------------------------------------------------ main
    def _backgrounds(self) -> list[_BG]:
        st = self.st
        dispcnt, mode = st.dispcnt, st.mode
        bgs: list[_BG] = []
        for i in range(4):
            if not dispcnt & (0x100 << i):
                continue
            cnt = st.reg(0x08 + 2 * i)
            if mode == 0 or (mode == 1 and i < 2):
                bgs.append(_BG(i, "text", cnt & 3, cnt))
            elif (mode == 1 and i == 2) or (mode == 2 and i >= 2):
                bgs.append(_BG(i, "affine", cnt & 3, cnt))
            elif mode in (3, 4, 5) and i == 2:
                bgs.append(_BG(i, "bitmap", cnt & 3, cnt))
        return bgs

    def render_split(self, st: GBAState, hide_oam=frozenset(), front_of: tuple[int, int] | None = None,
                     hud_bgs=frozenset()) -> SplitResult:
        """Render at 1x as separate layers, so a client can draw its own models in between.

        * behind: the frame without the `hide_oam` sprites and without the `hud_bgs` layers
        * front:  RGBA; opaque where the GBA would draw something *in front of* a sprite with
                  priority/OAM slot `front_of` = (priority, slot): BGs with a lower priority
                  number, sprites with a lower priority number or the same priority and a
                  lower slot. None if nothing is in front (or `front_of` is None).
        * hud:    RGBA; opaque where one of `hud_bgs` (screen UI such as text boxes) is on top.

        Drawing behind, then the models, then front, then hud reproduces the game's layer order.
        """
        if st.dispcnt & 0x80:
            return SplitResult(np.full((H, W, 3), 255, np.uint8))
        self._prepare(st)
        bgs = self._backgrounds()
        sprites = self._plan_sprites(hide_oam)
        rgb, _ = self._evaluate(bgs, sprites, self._grid, hires=False)
        top_bit, top_prio, top_slot = self._top
        hud = np.isin(top_bit, list(hud_bgs)) if hud_bgs else np.zeros(len(top_bit), bool)
        front = None
        if front_of is not None:
            prio, slot = front_of
            in_front = (((top_bit < LAYER_OBJ) & ~hud & (top_prio < prio)) |
                        ((top_bit == LAYER_OBJ) & ((top_prio < prio) | ((top_prio == prio) & (top_slot < slot)))))
            if in_front.any():
                front = _rgba(rgb, in_front)
        hud_img = _rgba(rgb, hud) if hud.any() else None
        behind = rgb
        if hud_img is not None:
            behind, _ = self._evaluate([bg for bg in bgs if bg.bit not in hud_bgs], sprites, self._grid, hires=False)
        return SplitResult(behind.reshape(H, W, 3), front, hud_img)

    def render(self, st: GBAState, hide_oam=frozenset()) -> RenderResult:
        """Render `st`. OAM slots in `hide_oam` are left out (e.g. sprites an entity renderer replaces)."""
        S = self.scale
        if st.dispcnt & 0x80:  # forced blank
            return RenderResult(np.full((H * S, W * S, 3), 255, np.uint8))
        self._prepare(st)
        bgs = self._backgrounds()
        sprites = self._plan_sprites(hide_oam)

        rgb, touched = self._evaluate(bgs, sprites, self._grid, hires=False)
        image = rgb.reshape(H, W, 3)
        result = RenderResult(image, [s.info for s in sprites],
                              sum(len(bg.atlas) for bg in bgs if bg.atlas is not None))
        if S > 1:
            result.image = image.repeat(S, 0).repeat(S, 1)
        if touched is None or not touched.any():
            return result

        dirty = touched.reshape(H, W)
        # Hi-res samples of a rotated/scaled BG can reach a few texels away from the 1x sample.
        reach = 0
        for bg in bgs:
            if bg.kind == "affine" and bg.atlas is not None:
                base = 0x20 + (bg.bit - 2) * 0x10
                pa, pb, pc, pd = (abs(st.reg_s16(base + k)) for k in (0, 2, 4, 6))
                reach = max(reach, -(-max(pa + pb, pc + pd) // 256))
        if reach:
            dirty = _dilate(dirty, min(reach, 4))
        result.hires_pixels = int(dirty.sum())
        hmask = dirty.repeat(S, 0).repeat(S, 1) if S > 1 else dirty
        Yd, Xd = np.nonzero(hmask)
        rgb_hi, _ = self._evaluate(bgs, sprites, _Points(Xd.astype(np.int64), Yd.astype(np.int64), S), hires=True)
        result.image[Yd, Xd] = rgb_hi
        return result
