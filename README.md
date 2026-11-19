# gba-shadow

Prototype that runs a GBA game in mGBA and draws a second, "shadow" view of it
from the live display state (sprite attributes, tiles, palettes, scroll
registers), so that graphics can be swapped for your own art at higher
resolution.

```
┌──────────── mGBA ────────────┐   TCP 127.0.0.1:61337   ┌──────── python -m shadow view ────────┐
│ game runs as normal           │  ~2 KiB/frame (diffed)  │ mirror of IO/palette/VRAM/OAM          │
│ lua/shadow_bridge.lua reads   │ ──────────────────────► │ renderer: exact 1x re-render           │
│ IO regs, palette, VRAM, OAM   │                         │  + replacement art from a pack at S×   │
│ once per frame                │                         │ pygame window                          │
└───────────────────────────────┘                         └────────────────────────────────────────┘
```

![left: mGBA, right: shadow view at 4x with a demo pack](docs/demo.png)

*Left: mGBA's output. Right: the shadow view at 4x with an auto-generated demo
pack (Scale2x + outlines). The test ROM's graphics are deliberately noisy test
patterns.*

## What works

- **Bridge** (`lua/shadow_bridge.lua`): mGBA Lua script that streams the display
  state every frame. It sends only the 1 KiB chunks that changed, never blocks
  the emulator (frames are dropped while the viewer is busy), and reconnects
  automatically. Cost inside mGBA is about 1 ms/frame.
- **Reference renderer** (`shadow/render.py`): rebuilds the frame from that
  state. It covers modes 0–5, text and affine BGs, 4bpp/8bpp, 1D/2D OBJ mapping,
  normal/affine/double-size sprites, flips, priorities, windows (WIN0/WIN1/OBJ
  window) and alpha, brighten and darken blending. **At 1x it matches mGBA pixel for pixel**
  (122/122 captured test frames).
- **Content-hashed replacements** (`shadow/pack.py`): every sprite and 8x8 BG tile is
  identified by a hash of its pixels (+ a hash of its palette), not by OAM slot or
  VRAM address, which games reshuffle constantly. Drop a PNG named by that hash into
  a pack and it is drawn instead, at any resolution.
- **Dump mode**: writes every never-seen-before sprite/tile as a PNG named by
  its hashes, so you can collect a game's graphics just by playing it.
- **Viewer** (`python -m shadow view`): live window with sprite-box overlay,
  replacement toggle and screenshots.

## Quick start

Requirements: mGBA **0.10 or newer** (needs scripting support), Python 3.10+.

```bash
pip install -r requirements.txt
```

```bash
python -m shadow view
```

Then in mGBA, load your ROM and open **Tools → Scripting… → File → Load script**,
and pick `lua/shadow_bridge.lua`. The view appears within a second. The order doesn't
matter: the script retries the connection every 60 frames.

Viewer keys: **B** sprite boxes (green = replaced, yellow = original, blue = OBJ window),
**R** toggle replacements, **S** screenshot, **Esc** quit.

## Making replacement graphics

1. Play the game with dumping on:

   ```bash
   python -m shadow view --dump dumps/mygame
   ```

   Add `--dump-tiles` to collect BG tiles too. Every sprite lands in
   `dumps/mygame/sprites/<pixhash>-<palhash>.png`, and `index.csv` records the
   first frame each one appeared in.
2. Copy the files you want to replace into a pack, then redraw them at any size.
   They are resized to *sprite size × scale* when loaded:

   ```
   packs/mygame/
     pack.json            {"scale": 4}
     sprites/<pixhash>-<palhash>.png   ← only this palette variant
     sprites/<pixhash>.png             ← every palette (rename to drop the -palhash)
     tiles/<pixhash>-<palhash>.png     ← one 8x8 BG tile, drawn at 32x32 for scale 4
   ```

   Alpha < 128 is transparent. Flips, rotation/scaling, priorities, windows and
   blending are applied to the replacement exactly as to the original.
3. View with the pack:

   ```bash
   python -m shadow view --pack packs/mygame
   ```

`tools/make_demo_pack.py DUMP_DIR PACK_DIR` turns a dump into a test pack
(Scale2x + outlines). It's useful for checking the pipeline before you have real art.

## Checking the renderer against mGBA

The bridge can also save full snapshots plus mGBA's own screenshot of the same
frame. Set `capture_dir` in the `CONFIG` table at the top of the script (or set
the `SHADOW_CAPTURE_DIR` / `SHADOW_CAPTURE_EVERY` / `SHADOW_CAPTURE_COUNT`
environment variables for mGBA), then:

```bash
python -m shadow verify path/to/captures
```

Mismatching frames get a `diff/` image (mGBA | ours | difference). Running this
against your target game is the quickest way to find features it uses that the
renderer doesn't handle yet. Also useful:

```bash
python -m shadow render capture.gbsh -o out.png --scale 4 --pack packs/mygame
```

## Test ROM, headless runner, tests

No commercial ROM is needed for development:

- `testrom/`: a small homebrew ROM (C, built with the stock `gcc-arm-none-eabi`)
  that cycles through every video mode and the sprite, window and blending features
  above. A prebuilt `shadowtest.gba` is included.
- `harness/shadow-run`: runs a ROM plus a Lua script under libmgba with no GUI,
  using the same scripting API as mGBA's Qt frontend. It's used for automated
  captures and the end-to-end test.

```bash
sudo apt install gcc-arm-none-eabi libmgba-dev mgba-qt   # Debian/Ubuntu
```

```bash
make            # builds testrom/shadowtest.gba and harness/shadow-run
```

```bash
make selftest   # captures ~120 frames headlessly and verifies them against mGBA
```

```bash
make test       # pytest: renderer vs mGBA fixtures, packs/dumps, protocol, bridge end-to-end
```

## Performance (this machine, NumPy renderer)

| configuration                          | ms / frame |
|----------------------------------------|-----------:|
| 1x, no pack                            | ~13        |
| 4x, no pack (upscaled 1x)              | ~15        |
| 4x, all sprites replaced               | ~65        |
| 2x, all sprites + every BG tile        | ~60        |
| 4x, all sprites + every BG tile        | ~250       |

The renderer computes the exact 1x frame first and then redoes, at high
resolution, only the pixels covered by replacement art. So cost grows with how
much is replaced, not with output size. Sprite-only packs are usable at 2x–3x,
but a full HD pack at 60 fps needs the GPU port listed below.

## Known limitations / next steps

- **Mid-frame effects are lost.** State is sampled once per frame (at VBlank), so
  HBlank-DMA/IRQ raster effects (wavy water, per-line scroll, Mode 7 floors, split
  HUD palettes) render with the last value written. Fixing this means capturing
  per scanline, either by embedding libmgba and hooking its scanline renderer or
  by having the renderer replay a per-line register log.
- **GPU renderer.** The NumPy renderer is the reference (exact, easy to test).
  For real-time HD, port the same rules to a fragment shader: VRAM, palette and
  OAM as textures, replacements as an atlas.
  The hashing/plan step stays on the CPU.
- **Bitmap modes (3–5)** aren't replaceable (there are no tiles to identify), and
  **mosaic** and the per-line sprite pixel budget aren't emulated.
- **Metasprites.** Replacements are per OAM entry. Treating a multi-sprite
  character as one image needs grouping, or reading the game's own object
  table from RAM. That's also where 3D models would hook in.
- Tested headlessly against libmgba 0.10.2 (same scripting engine as the GUI),
  not yet inside the mGBA Qt app on a desktop.

## Layout

```
lua/shadow_bridge.lua   mGBA script: state streaming + capture mode
shadow/                 Python package (python -m shadow …)
  protocol.py           wire format, TCP receiver, capture loading
  state.py              GBA display state + register accessors
  render.py             reference renderer + replacement compositing
  pack.py               hashing, replacement packs, dumper
  viewer.py             pygame window
  verify.py             renderer-vs-mGBA comparison
tools/make_demo_pack.py demo pack generator
testrom/                homebrew test ROM (C)
harness/                headless libmgba runner (C)
tests/                  pytest suite + mGBA capture fixtures
```

### Wire format

Little endian. Header `"GBSH" u8 version u8 flags u16 count u32 frame u32 payload_len`,
followed by `count` records of `u8 region u32 offset u32 length` + bytes. Regions:
0 = I/O registers (first 0x60 bytes), 1 = palette, 2 = VRAM, 3 = OAM. Flag bit 0 marks a
full snapshot; `.gbsh` capture files are a single full-snapshot message.
