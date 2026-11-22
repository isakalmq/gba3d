# CLAUDE.md

Notes for Claude working in this repo. The README explains the project for people;
this file is the working checklist.

## Project in one paragraph

`lua/shadow_bridge.lua` runs inside mGBA (or `player/shadow-play`) and streams the GBA's
display state plus requested RAM to Python (`shadow/`). Python re-renders it
(`render.py`, exact vs mGBA at 1x), turns RAM into entities through per-game adapters
(`games/`), builds 3D maps (`tilemap.py`), and publishes everything over WebSocket
(`server.py`) to the three.js client (`web/index.html`). `./start.py` runs it all.

## Commands

```bash
python3 -m pytest -q          # all tests (some need harness/ and testrom/ built: make)
make selftest                 # renderer vs mGBA on ~120 captured test-ROM frames
python3 -m pyflakes shadow tests tools start.py
```

## Screenshots (for checking models, maps and the client)

Use these instead of ad-hoc scripts. Output goes to `build/` (git-ignored).
Look at the PNGs with the Read tool.

### 1. A Pokémon Emerald map without a ROM: `tools/emerald_fixture.py`

Builds a realistic game state for any map from the pret/pokeemerald decompilation:
- live map grid in RAM
- a small fake ROM with the tileset tables
- tiles and palettes in VRAM
- BG tilemaps filled like the game's `DrawMetatile`
- a placeholder player sprite (magenta 16x32, OAM slot 3) at a chosen tile

```bash
python3 tools/emerald_fixture.py LittlerootTown_BrendansHouse_1F --player 5,4 -o build/mom1f.png
```

It writes the original 2D picture (3x) and prints how many pixels the automatic 3D diorama
(without props) differs from it when seen from the game's view. **This must be 0**;
anything else is a bug in `tilemap.py` or the adapter. Props deliberately replace the
original art, so check them with screenshots instead.

The fake ROM puts the map's Tileset structs at their **real addresses**, taken from the
decomp's symbol map (`symbols` branch, fetched into `build/decomp/pokeemerald-symbols`).
That's because the prop kit (`PROP_KIT` in `shadow/games/pokeemerald.py`) identifies
tilesets by ROM address. If props don't show up in a fixture screenshot, check that
first. Layout names are the folder names in the decomp's
`data/layouts/` (e.g. `LittlerootTown_BrendansHouse_2F`, `LittlerootTown`).

The decomp is sparse-cloned on demand into `build/decomp/pokeemerald` (only the files a
map needs). **Never commit anything from it, or any ROM**: the graphics are the game's.

### 2. The web client: `tools/screenshot.py`

```bash
# fixture map, 3D view, plus turned views (yaw,pitch in radians)
python3 tools/screenshot.py LittlerootTown_BrendansHouse_1F --player 5,4 --orbit 0.6,-0.25 --orbit -0.55,-0.35
# same map in the 2D picture view; player facing left (1 down, 2 up, 3 left, 4 right)
python3 tools/screenshot.py LittlerootTown_BrendansHouse_2F --view 2d --facing 3
# a server that is already running (./start.py with a game, or the test ROM below)
python3 tools/screenshot.py --url http://127.0.0.1:8765
```

Writes `build/screenshots/<name>_<view>.png`, one `_<n>.png` per `--orbit`, and a
`_montage.png` with all of them side by side. It prints the client's status line and
any page errors; treat page errors as failures.

How it works:
1. The fixture is served by the real `FrameServer` through a stub session, so the
   page receives exactly what a live game sends: map/atlas messages, frames and
   entities.
2. The page is the real client in headless Chromium, using Node Playwright with
   SwiftShader WebGL.
3. Turned views set `window.gbaShadow.orbit` (`hold = true` keeps it from easing back).

### 3. Live screenshots from the test ROM (no Emerald needed)

```bash
make && make player
python3 -m shadow serve --port 61360 --http-port 8780 &
SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy SHADOW_PLAY_EXIT_AFTER=2400 SHADOW_PORT=61360 \
  player/shadow-play testrom/shadowtest.gba lua/shadow_bridge.lua &
python3 tools/screenshot.py --url http://127.0.0.1:8780/ --view 2d
```

The test ROM's first ~70 frames of each scene are blank (forced blank while it sets
up). Its "player" (sprite 0) moves 2 px per frame. There's no 3D map for it, so the
client falls back to 2D.

### Debug hooks in the client (`window.gbaShadow`)

- `picture = false`: hides the game picture / 3D map, so only models are left. Use
  this to measure a model's pixels on its own.
- `orbit.{yaw, pitch, hold}`: turns the 3D view.
- `updateActors([...entities])`: places models without a game running.
- `view`: the view the latest frame was rendered in (`"3d"` or `"2d"`).
- `window.lastMeta`: the latest frame's JSON (entities, `drawn` boxes, camera).

### Measuring things in a screenshot

The 240x160 picture is letterboxed into the viewport:

```
s = min(viewW / 240, viewH / 160)
left = (viewW - 240*s) / 2
top = (viewH - 160*s) / 2
gba = ((px - left) / s, (py - top) / s)
```

Compare a model's pixel box with the entity's `drawn` box from `window.lastMeta`. For
user screenshots, calibrate against a known object (a bed frame or a wall edge)
instead, because browser chrome shifts the viewport.

## Modelling loop (characters and props)

1. Edit `models/<kind>.blend`, then `make models BLENDER=/path/to/blender`. This
   writes `shadow/web/models/<kind>.glb`.
2. Map entity kinds to files in `MODEL_FILES` in `shadow/web/index.html`. Models
   face +z, with their feet at y = 0. They are scaled uniformly so their on-screen
   silhouette fills the sprite's drawn box. Clips named `Walk` play while moving.
3. Check the fit with `tools/screenshot.py <map> --facing N` for all four facings,
   in both `--view 3d` and `--view 2d`. Add `--orbit` shots to see the model from
   the side.
4. Props (furniture replacing metatiles):
   - Add a `PropDef` to `PROP_KIT` (tileset address + metatile id pattern + anchor +
     facing); see `shadow/props.py` and the README's prop kit section.
   - `tools/model_props.py` / `make props-blend` build the prop .blend files from
     code; `make models` exports them.
   - Check with a fixture screenshot of a map that uses them, in the game's view
     (should cover about the same pixels as the original art) and with `--orbit`.
5. For maps: the automatic diorama must stay pixel-exact in the game's view (the
   fixture tool prints the count).

## Cloud sandbox notes

- `cdn.jsdelivr.net` is blocked. `tools/screenshot.py` then fetches three.js from
  the npm registry into `build/three/` and serves it to the page.
- GitHub clones work through the git proxy (used for the decomp).
- No display: SDL programs need `SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy`.
  Chromium needs `--use-gl=swiftshader` (the tool passes it).
- Commits must be authored as `Claude <noreply@anthropic.com>` (a stop hook checks it).
  Push to the PR branch after committing.
