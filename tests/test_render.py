"""Renderer tests. Fixtures are mGBA captures of testrom/shadowtest.gba (state + screenshot)."""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from shadow.pack import Dumper, ReplacementPack
from shadow.protocol import load_capture
from shadow.render import Renderer

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURES = sorted(FIXTURES.glob("*.gbsh.gz"))


def reference(path: Path) -> np.ndarray:
    return np.asarray(Image.open(FIXTURES / (path.name.split(".")[0] + ".png")).convert("RGB"))


def first_capture(predicate):
    for p in CAPTURES:
        st = load_capture(p)
        if predicate(st):
            return st
    pytest.skip("no matching fixture")


@pytest.mark.parametrize("path", CAPTURES, ids=lambda p: p.name.split(".")[0])
def test_matches_mgba_exactly(path):
    st = load_capture(path)
    ours = Renderer(scale=1).render(st).image
    np.testing.assert_array_equal(ours, reference(path))


def test_upscale_without_pack_is_nearest_neighbour():
    st = first_capture(lambda s: s.mode == 1)
    one = Renderer(scale=1).render(st).image
    three = Renderer(scale=3).render(st).image
    np.testing.assert_array_equal(three, one.repeat(3, 0).repeat(3, 1))


def _write(path: Path, rgba: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba.astype(np.uint8), "RGBA").save(path)


def test_sprite_replacement_hires(tmp_path):
    st = first_capture(lambda s: s.mode == 0 and not s.dispcnt & 0x80)
    base = Renderer(scale=2).render(st)
    target = next(s for s in base.sprites if s.mode == 0 and not s.affine and s.priority == 0)
    w, h = target.width, target.height
    solid = np.zeros((h * 2, w * 2, 4), np.uint8)
    solid[...] = (255, 0, 255, 255)
    _write(tmp_path / "sprites" / f"{target.pixhash}.png", solid)

    res = Renderer(scale=2, pack=ReplacementPack(tmp_path)).render(st)
    info = next(s for s in res.sprites if s.index == target.index)
    assert info.replaced
    magenta = np.all(res.image == (255, 0, 255), axis=-1)
    assert magenta.sum() > 0
    # Everything outside replaced sprites' boxes is untouched.
    outside = np.ones(magenta.shape, bool)
    for s in res.sprites:
        if s.replaced:
            outside[max(0, s.y * 2):max(0, (s.y + s.height) * 2), max(0, s.x * 2):max(0, (s.x + s.width) * 2)] = False
    np.testing.assert_array_equal(res.image[outside], base.image[outside])


def test_palette_specific_file_wins(tmp_path):
    st = first_capture(lambda s: s.mode == 0 and not s.dispcnt & 0x80)
    target = next(s for s in Renderer().render(st).sprites if s.mode == 0 and not s.affine and s.priority == 0)
    generic = np.zeros((target.height, target.width, 4), np.uint8)
    generic[...] = (0, 255, 0, 255)
    specific = generic.copy()
    specific[...] = (255, 0, 255, 255)
    _write(tmp_path / "sprites" / f"{target.pixhash}.png", generic)
    _write(tmp_path / "sprites" / f"{target.pixhash}-{target.palhash}.png", specific)
    res = Renderer(scale=1, pack=ReplacementPack(tmp_path)).render(st)
    img = res.image
    x0, y0 = max(0, target.x), max(0, target.y)
    box = img[y0:target.y + target.height, x0:target.x + target.width]
    assert np.all(box == (255, 0, 255), axis=-1).any()
    assert not np.all(box == (0, 255, 0), axis=-1).any()
    # Same pixels drawn with another palette fall back to the generic file.
    others = [s for s in res.sprites if s.pixhash == target.pixhash and s.palhash != target.palhash]
    assert all(s.replaced for s in others)
    assert np.all(img == (0, 255, 0), axis=-1).any() == bool(others)


def test_tile_dump_and_replacement(tmp_path):
    st = first_capture(lambda s: s.mode == 0 and not s.dispcnt & 0x80)
    dumper = Dumper(tmp_path / "dump", tiles=True)
    Renderer(scale=1, dumper=dumper).render(st)
    dumper.close()
    tiles = sorted((tmp_path / "dump" / "tiles").glob("*.png"))
    sprites = sorted((tmp_path / "dump" / "sprites").glob("*.png"))
    assert tiles and sprites
    assert (tmp_path / "dump" / "index.csv").read_text().count("\n") == len(tiles) + len(sprites) + 1

    # Replace every dumped tile with solid cyan at 4x: BG pixels must turn cyan.
    for t in tiles:
        solid = np.zeros((32, 32, 4), np.uint8)
        solid[...] = (0, 255, 255, 255)
        _write(tmp_path / "pack" / "tiles" / t.name, solid)
    res = Renderer(scale=4, pack=ReplacementPack(tmp_path / "pack")).render(st)
    assert res.tiles_replaced > 0
    assert res.hires_pixels > 0
    cyan = np.all(res.image == (0, 255, 255), axis=-1).mean()
    assert cyan > 0.2


def _scene0_captures():
    return [p for p in CAPTURES if load_capture(p).dispcnt == 0x3F40]   # mode 0, WIN0 keeps OBJ visible


@pytest.mark.parametrize("path", _scene0_captures(), ids=lambda p: p.name.split(".")[0])
def test_split_front_layer_follows_hardware_order(path):
    st = load_capture(path)
    r = Renderer()
    infos = r.render(st).sprites
    checked = 0
    for target in [s for s in infos if s.mode == 0]:
        slot = target.index
        prio = target.priority
        r.render(st)
        top_bit, top_prio, top_slot = r._top
        visible = (top_bit == 4) & (top_slot == slot)
        # Hardware quirk: a transparent pixel of a better-priority sprite in a later slot raises
        # the effective priority of what is under it. The split uses the sprite's own priority,
        # so leave those few pixels out.
        quirk = visible & (top_prio != prio)
        visible &= ~quirk
        r._prepare(st)
        only = [s for s in r._plan_sprites() if s.info.index == slot]
        drawn = r._sample_sprites(only, r._grid, False)[1]
        split = r.render_split(st, hide_oam={slot}, front_of=(prio, slot))
        alpha = split.front[..., 3].ravel() if split.front is not None else np.zeros(drawn.shape, np.uint8)
        assert (alpha[visible] == 0).all(), f"slot {slot}: front layer covers visible sprite pixels"
        hidden = drawn & ~visible & ~quirk
        assert (alpha[hidden] == 255).all(), f"slot {slot}: hidden sprite pixels not covered"
        checked += int(hidden.any())
    assert checked, "expected at least one partly covered sprite"


def test_split_hud_layer():
    st = load_capture(_scene0_captures()[0])
    split = Renderer().render_split(st, hide_oam={1}, front_of=(1, 1), hud_bgs={3})
    without_bg3 = st.copy()
    without_bg3.io[1] &= 0xF7                   # DISPCNT: BG3 off
    np.testing.assert_array_equal(split.behind, Renderer().render(without_bg3, hide_oam={1}).image)
    r = Renderer()
    r.render(st, hide_oam={1})
    assert split.hud is not None
    np.testing.assert_array_equal(split.hud[..., 3].ravel() == 255, r._top[0] == 3)


def test_split_reports_drawn_bounds_of_sprites():
    st = load_capture(_scene0_captures()[0])
    r = Renderer()
    r._prepare(st)
    planned = {sp.info.index: sp for sp in r._plan_sprites()}
    slots = [i for i, sp in planned.items() if not sp.info.affine and sp.info.mode == 0]
    split = r.render_split(st, bounds_for={i: [i] for i in slots})
    for i in slots:
        sp = planned[i]
        alpha = sp.src1[..., 3] > 0
        if sp.vflip:
            alpha = alpha[::-1]
        if sp.hflip:
            alpha = alpha[:, ::-1]
        ys, xs = np.nonzero(alpha)
        x0, y0 = sp.info.x + xs.min(), sp.info.y + ys.min()
        x1, y1 = sp.info.x + xs.max() + 1, sp.info.y + ys.max() + 1
        expected = (max(0, x0), max(0, y0), min(240, x1), min(160, y1))
        assert split.bounds[i] == expected, i
