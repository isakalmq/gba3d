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
- **Entities** (`shadow/games/`): per-game adapters read the game's own objects
  from work RAM and turn them into entities (position, tile, facing, moving, and
  the sprite slots that draw them). Included: **Pokémon Emerald (US), player
  only**, and the test ROM.
- **WebSocket output + 3D demo** (`python -m shadow serve`): publishes every
  frame plus its entities, split into layers behind and in front of the player
  plus UI. A three.js page shows the game from its original point of view, with
  a cube in place of each entity's sprite, drawn in the game's own layer order.

## Quick start

Requirements: Python 3.10+ and, on Ubuntu/Debian:

```bash
sudo apt install mgba-qt libmgba-dev libsdl2-dev
```

```bash
pip install -r requirements.txt
```

Put your ROM in `roms/` (git ignores everything in there), then:

```bash
./start.py
```

That builds `player/shadow-play` on first use, starts the WebSocket server,
launches the game with the bridge already loaded, and opens the 3D web client.
Close the game window to stop everything. Options:

```bash
./start.py emerald          # pick a ROM by part of its file name, if roms/ has several
./start.py --view           # pygame viewer instead of the web client
./start.py --dump dumps/mygame   # viewer + dump new sprites (see below)
./start.py --pack packs/mygame   # viewer with your replacement graphics
./start.py --emulator mgba  # use the mGBA app instead of shadow-play
```

**shadow-play** is a minimal player built on libmgba, the library under mGBA, so
the emulation is the same. It exists because mGBA 0.10 can't load a Lua script
from the command line. Controls: arrows, **X** = A, **Z** = B, **A**/**S** =
L/R, **Enter** = Start, **Backspace** = Select, hold **Tab** to fast-forward,
**F5**/**F8** to save/load a state, **Esc** to quit. Game controllers work too.
Battery saves go next to the ROM, as in mGBA, so mGBA can read them as well.

**With the mGBA app** (`--emulator mgba`, or if shadow-play can't be built),
load the script by hand: **Tools → Scripting… → File → Load script** and pick
`lua/shadow_bridge.lua`. The order doesn't matter: the script retries the
connection every 60 frames. To run the pieces separately, start
`python -m shadow serve` (or `view`) first.

Viewer keys: **B** sprite boxes (green = replaced, yellow = original, blue = OBJ window),
**E** entity overlay, **H** hide entity sprites, **R** toggle replacements,
**S** screenshot, **Esc** quit.

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

## Entities and 3D

```bash
python -m shadow serve
```

![three.js client on the test ROM: a cube replaces the player sprite; left, layers that are in front of the player (priority-0 tiles and a sprite) still cover it; right, the same frame with that layer switched off](docs/3d-client.png)

Load the bridge in mGBA as usual (or use `./start.py`, which does it all) and
open http://127.0.0.1:8765. The page shows the game from the original point of
view, with the player's sprite replaced by a cube. The cube has a "nose" showing
which way the player faces, and it bobs while walking. The panel shows the live
entity data and has switches for each layer. The page loads three.js from
jsDelivr, so the browser needs internet access.

**Draw order.** The game picture isn't one flat image. The server splits it the
way the GBA layers it, relative to the player's sprite:

- **behind:** everything the GBA draws behind the player
- **front:** what it draws in front of the player: map layers with a lower
  priority number, such as Emerald's bed covers, roofs and treetops, and other
  sprites with better priority or an earlier sprite slot
- **hud:** layers the adapter marks as screen UI (Emerald: BG0, the text boxes and menus)

The client draws behind, then the 3D models, then front, then hud, which is
the game's own order. Priorities are read from the player's sprite every frame,
so elevation changes (bridges, where Emerald raises the player to priority 1)
are handled too.

**Camera.** The picture is drawn exactly as on the GBA, letterboxed to the
window. Models are drawn by a second camera with the same scale (1 unit = 1
GBA pixel) that looks down at the 3/4 angle of the game's art (adjustable in the
panel). Each model is fitted over the rectangle the game draws the character
in: the server measures the opaque pixels of the entity's sprites every frame
(`drawn` in the JSON). In the 3/4 view, the bottom of that rectangle is the front
of the character's footprint and the top is the top of its head. So the model is
as wide as the art, its front edge sits on the bottom line, and its height is
chosen so its top reaches the top of the art at the current view angle. The
model then covers the same pixels as the original sprite, and the layers in
front of it line up the same way.

In the pygame viewer (`python -m shadow view`) the same entities are drawn as
an overlay: **E** toggles the overlay and **H** hides the entities' sprites.

**How it fits together**

1. When the bridge connects it sends a *hello* with the ROM's game code
   (e.g. `BPEE`).
2. The renderer looks up an adapter for that code (`shadow/games/`) and replies
   with the work-RAM ranges the adapter needs. The bridge then streams those
   ranges every frame, diffed like video memory.
3. Each frame, the adapter turns the mirrored RAM into `Entity` objects
   (`shadow/entities.py`):

   | field    | meaning |
   |----------|---------|
   | `id`, `kind` | stable id and type: `"player"`; Emerald NPCs are `"<kind>:<mapGroup>.<mapNum>.<localId>"` with kind `"mom"` or `"npc"` |
   | `screen` | anchor point on the GBA screen (middle of the feet) |
   | `pos`    | the same point in world pixels (Emerald: map pixels, origin at the map's top-left) |
   | `tile`   | the tile the game thinks the entity is on |
   | `facing`, `moving` | `"down"`/`"up"`/`"left"`/`"right"`, walking or not |
   | `size`   | sprite size in GBA pixels |
   | `oam`    | hardware sprite slots that draw this entity (what to hide) |
   | `props`  | everything else the adapter knows |

4. `serve` sends one binary WebSocket message per frame on `/ws`:
   `u32 json_length | JSON`, then for each name in `JSON.layers`
   (`behind`, `front`, `hud`) a `u32 size` and that many bytes of 240x160
   RGBA. A size of 0 means the layer is empty this frame. The JSON holds the
   entity list, camera, game code, frame number, `hidden_oam` (the sprite slots
   removed), `front_of` (the entity, priority and slot the split was made
   for) and, per entity, `drawn`: the screen box `[x0, y0, x1, y1]` of its
   original sprite pixels. Send `{"hide_entity_sprites": false}` to get one unsplit frame with the
   original sprites instead. Any engine (Godot, Unity, Bevy, three.js...) can
   consume this. `shadow/server.py` has `decode_frame()` for Python clients.

**Timing.** Pokémon, like most games, runs its logic and then copies sprite
positions to the hardware in the *next* VBlank. RAM read at VBlank is therefore
one frame ahead of the picture. Adapters declare this (`ram_delay = 1`), and the
bridge then sends RAM from the previous frame along with the current picture.
The test ROM does the same, and a test checks that with the delay the entity
matches its sprite on every frame (and without it, it doesn't).

**Pokémon Emerald adapter** (`shadow/games/pokeemerald.py`)

- US English Emerald only (game code `BPEE`). Addresses come from the
  [pret/pokeemerald](https://github.com/pret/pokeemerald) decompilation's symbol
  map. Other languages need their own addresses.
- The player (always the first entity) and every other active object event whose
  sprite is on screen, only while the overworld is running (`gMain.callback2 ==
  CB2_Overworld`). There are no entities in battles or menus. NPC kinds come from
  their graphics id (`KIND_BY_GRAPHICS_ID`: 215 is `"mom"`; everything else is
  `"npc"`, with `graphics_id` and `local_id` in `props`).
- `pos` is exact and smooth. It's derived from the sprite position and the
  camera's scroll counters the same way the game places field sprites. It
  changes frame of reference when you cross into a connected map (the `map`
  prop changes at the same moment).
- `props` include the previous tile, bike/surf/dash flags, elevation, graphics
  id, ledge-jump offset and the sprite's animation number and frame.
- The decompilation's struct layouts and formulas are covered by tests with
  hand-built memory. **It hasn't been run against a real Emerald ROM yet**, so
  expect to adjust things on the first real run. `python -m shadow entities
  capture.gbsh --game BPEE` prints what the adapter sees in a capture (set
  `SHADOW_WATCH` for captures, see the bridge's header).

**Writing an adapter.** Add a module to `shadow/games/` with an object that has
`name`, `game_codes`, `ram_ranges` (bus address, length), `ram_delay`, and
`extract(state) -> (entities, camera)`. Read memory with `state.u8/s16/u32(...)`
at bus addresses: reading anything not in `ram_ranges` raises, so missing
ranges show up immediately. Then register it in `shadow/games/__init__.py`.
`shadow/games/shadowtest.py` is the smallest example.

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
  above. It also keeps a one-entry entity table in RAM for testing adapters.
  A prebuilt `shadowtest.gba` is included.
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
make test       # pytest: renderer vs mGBA fixtures, packs/dumps, protocol, entities, bridge end-to-end
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
- **Layering approximations.** The front/behind split is made relative to one
  entity (the player). Other entities' models are ordered against each other
  by 3D depth. Layers that are alpha-blended over the player (some text boxes)
  cover the model fully instead of blending. Screen fades done through
  palettes (most of Emerald's) darken the picture but not the models. The
  server would need to pass the fade level along for that.
- **More entities.** The Emerald adapter covers the player and NPCs, but only
  the mom has a kind (and a model) of her own. Next: more kinds, and a RAM
  inspector tool for games without a decompilation.
- **Metasprites.** Sprite *replacements* are still per OAM entry. Entities
  already know all the slots that draw them, so whole-character replacements
  can build on that.
- Tested headlessly against libmgba 0.10.2 (same scripting engine as the GUI),
  not yet inside the mGBA Qt app on a desktop.

## Layout

```
start.py                starts renderer + emulator + browser
roms/                   your ROMs (git-ignored)
lua/shadow_bridge.lua   mGBA script: state + RAM streaming, capture mode
shadow/                 Python package (python -m shadow …)
  protocol.py           wire format, TCP receiver (hello/watch), capture loading
  state.py              GBA display state, watched RAM, register accessors
  render.py             reference renderer + replacement compositing
  pack.py               hashing, replacement packs, dumper
  entities.py           Entity / World model, adapter interface
  games/                game adapters (pokeemerald.py, shadowtest.py)
  session.py            bridge connection + adapter selection
  server.py             WebSocket server (python -m shadow serve)
  web/index.html        three.js demo client
  viewer.py             pygame window
  verify.py             renderer-vs-mGBA comparison
tools/make_demo_pack.py demo pack generator
testrom/                homebrew test ROM (C)
harness/                headless libmgba runner (C)
player/                 shadow-play: libmgba + SDL2 player that loads the bridge at startup (C)
tests/                  pytest suite + mGBA capture fixtures
```

### Wire format

Little endian. Header `"GBSH" u8 version u8 flags u16 count u32 frame u32 payload_len`,
followed by `count` records of `u8 region u32 offset u32 length` + bytes. Regions:
0 = I/O registers (first 0x60 bytes), 1 = palette, 2 = VRAM, 3 = OAM, 4 = EWRAM,
5 = IWRAM (offsets relative to the region). Flag bit 0 marks a full snapshot. Flag bit 1
marks a *hello*: its payload is two length-prefixed strings (game code, title) and it
has no records. `.gbsh` capture files are a single full-snapshot message.

The renderer answers a hello with a watch request:
`"GBSW" u8 version u8 flags u16 count`, then `count` × `u32 bus_address u32 length`.
Flag bit 0 asks for RAM to be delayed by one frame. A new request replaces the old one.
