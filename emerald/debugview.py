"""Top-down debug client for the Emerald bridge (pygame).

Left: a mirror of the overworld built only from snapshots (collision grid, NPCs,
player, the current path). Right: the GBA's own screen. In SCREEN mode (battles,
menus, title) the GBA screen takes over the left side too: that is the fallback
until those screens get native versions.

Mouse:   left click   go to that tile (snapped to the nearest reachable tile)
         shift+left   go to exactly that tile
         right click  talk to the NPC there / interact with that tile
Keys:    arrows, Z = A, X = B, Enter = Start, Backspace = Select, A/S = L/R   raw input
         Space        advance text (in SCRIPT mode)
         0-9          choose a menu option
         Tab          toggle 4x speed
         F5 / F9      save / load state (next to the ROM)
         Esc          quit
"""
from __future__ import annotations

import collections
from pathlib import Path

import numpy as np

from shadow.core import (KEY_A, KEY_B, KEY_DOWN, KEY_L, KEY_LEFT, KEY_R, KEY_RIGHT, KEY_SELECT,
                         KEY_START, KEY_UP)

from . import controller as C
from . import data as D
from .host import EmeraldHost
from .snapshot import Mode

TILE = 20
MAP_W, MAP_H = 31, 25            # tiles shown around the player
PANEL_X = MAP_W * TILE + 12
GBA_ZOOM = 2
WIN_W = PANEL_X + 240 * GBA_ZOOM + 12
WIN_H = max(MAP_H * TILE, 160 * GBA_ZOOM + 330)

MODE_COLORS = {
    Mode.OVERWORLD: (80, 200, 120), Mode.SCRIPT: (240, 200, 80), Mode.DIRECT: (120, 170, 255),
    Mode.SCREEN: (230, 110, 230), Mode.TRANSITION: (150, 150, 150),
}


def _tile_colors(d: D.EmeraldData):
    mb = d.mb
    jumps = {mb[k] for k in ("MB_JUMP_EAST", "MB_JUMP_WEST", "MB_JUMP_NORTH", "MB_JUMP_SOUTH")}
    doors = {v for k, v in mb.items() if "DOOR" in k or "WARP" in k or k.endswith("_STAIRS")}
    return jumps, doors


class DebugView:
    def __init__(self, host: EmeraldHost, pack=None, state_path: Path | None = None):
        import pygame
        self.pg = pygame
        self.host = host
        self.d = host.data
        self.state_path = state_path
        self.pack = pack
        self.renderer = None
        if pack is not None:
            from shadow.render import Renderer
            self.renderer = Renderer(scale=pack.scale, pack=pack)
        self.events = collections.deque(maxlen=12)
        self.intents = collections.deque(maxlen=4)
        self.fast = False
        self.jumps, self.doors = _tile_colors(self.d)
        pygame.display.init()
        pygame.font.init()
        self.screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption("Emerald bridge debug view")
        self.font = pygame.font.Font(None, 18)
        self.small = pygame.font.Font(None, 15)
        self.origin = (0, 0)  # map coords of the top-left visible tile

    # -- input ----------------------------------------------------------------
    def raw_keys(self) -> int:
        pg = self.pg
        k = pg.key.get_pressed()
        m = {pg.K_UP: KEY_UP, pg.K_DOWN: KEY_DOWN, pg.K_LEFT: KEY_LEFT, pg.K_RIGHT: KEY_RIGHT,
             pg.K_z: KEY_A, pg.K_x: KEY_B, pg.K_RETURN: KEY_START, pg.K_BACKSPACE: KEY_SELECT,
             pg.K_a: KEY_L, pg.K_s: KEY_R}
        keys = 0
        for key, bit in m.items():
            if k[key]:
                keys |= bit
        return keys

    def tile_at(self, px: int, py: int) -> tuple[int, int] | None:
        if px >= MAP_W * TILE or py >= MAP_H * TILE:
            return None
        return self.origin[0] + px // TILE, self.origin[1] + py // TILE

    def submit(self, intent: C.Intent) -> None:
        self.host.submit(intent)
        self.intents.appendleft(intent)

    def handle(self, ev) -> bool:
        pg = self.pg
        snap = self.host.snapshot
        if ev.type == pg.QUIT:
            return False
        if ev.type == pg.KEYDOWN:
            if ev.key == pg.K_ESCAPE:
                return False
            if ev.key == pg.K_SPACE:
                self.submit(C.advance_text())
            elif pg.K_0 <= ev.key <= pg.K_9:
                self.submit(C.choose(ev.key - pg.K_0))
            elif ev.key == pg.K_TAB:
                self.fast = not self.fast
            elif ev.key == pg.K_F5 and self.state_path and hasattr(self.host.core, "save_state"):
                self.state_path.write_bytes(self.host.core.save_state())
                self.events.appendleft(f"saved state to {self.state_path.name}")
            elif ev.key == pg.K_F9 and self.state_path and self.state_path.exists():
                self.host.core.load_state(self.state_path.read_bytes())
                self.host.cancel("state_loaded")
                self.events.appendleft(f"loaded state {self.state_path.name}")
        if ev.type == pg.MOUSEBUTTONDOWN and snap.mode != Mode.SCREEN:
            t = self.tile_at(*ev.pos)
            if t is None:
                return True
            if ev.button == 1:
                exact = bool(pg.key.get_mods() & pg.KMOD_SHIFT)
                self.submit(C.goto(t[0], t[1], exact=exact))
            elif ev.button == 3:
                obj = next((o for o in snap.objects if (o.x, o.y) == t and not o.is_player), None)
                self.submit(C.interact(local_id=obj.local_id) if obj else C.interact(x=t[0], y=t[1]))
        return True

    # -- drawing --------------------------------------------------------------
    def gba_image(self) -> np.ndarray | None:
        core = self.host.core
        if self.renderer is not None and hasattr(core, "display_state"):
            return self.renderer.render(core.display_state()).image
        if hasattr(core, "video_rgb"):
            return core.video_rgb()
        return None

    def draw_map(self) -> None:
        pg, scr = self.pg, self.screen
        snap, grid = self.host.snapshot, self.host.grid
        pg.draw.rect(scr, (12, 12, 16), (0, 0, MAP_W * TILE, MAP_H * TILE))
        if grid is None or snap.player is None:
            scr.blit(self.font.render("no overworld yet", True, (160, 160, 160)), (10, 10))
            return
        p = snap.player
        ox, oy = p.x - MAP_W // 2, p.y - MAP_H // 2
        self.origin = (ox, oy)
        O = D.MAP_OFFSET
        mw, mh = grid.map_bounds()
        for ty in range(MAP_H):
            for tx in range(MAP_W):
                mx, my = ox + tx, oy + ty
                bx, by = mx + O, my + O
                if not (0 <= bx < grid.width and 0 <= by < grid.height):
                    continue
                beh = grid.behavior_at(bx, by)
                col = grid.collision(bx, by)
                elev = grid.elevation(bx, by)
                inside = 0 <= mx < mw and 0 <= my < mh
                if col:
                    c = (70, 70, 78)
                elif beh in self.d.surfable:
                    c = (40, 80, 170)
                elif beh in self.d.encounter:
                    c = (40, 120, 50)
                else:
                    c = (34 + elev * 6, 36 + elev * 6, 42 + elev * 6)
                if beh in self.doors:
                    c = (150, 90, 40)
                if not inside:
                    c = tuple(v // 2 for v in c)
                r = (tx * TILE, ty * TILE, TILE - 1, TILE - 1)
                pg.draw.rect(scr, c, r)
                if beh in self.jumps:
                    pg.draw.line(scr, (230, 160, 60), (r[0] + 3, r[1] + TILE - 5), (r[0] + TILE - 5, r[1] + TILE - 5), 3)
        # current path and goal
        it = self.host.controller.current
        if it is not None:
            path = it.state.get("path") or []
            for s in path:
                cx, cy = (s.x - ox) * TILE + TILE // 2, (s.y - oy) * TILE + TILE // 2
                pg.draw.circle(scr, (240, 220, 80), (cx, cy), 3)
            goal = it.state.get("goal") or it.state.get("approach")
            if goal:
                gx, gy = (goal[0] - ox) * TILE, (goal[1] - oy) * TILE
                pg.draw.line(scr, (255, 80, 80), (gx + 3, gy + 3), (gx + TILE - 4, gy + TILE - 4), 2)
                pg.draw.line(scr, (255, 80, 80), (gx + TILE - 4, gy + 3), (gx + 3, gy + TILE - 4), 2)
        # objects
        for o in snap.objects:
            cx, cy = (o.x - ox) * TILE + TILE // 2, (o.y - oy) * TILE + TILE // 2
            if not (0 <= cx < MAP_W * TILE and 0 <= cy < MAP_H * TILE):
                continue
            color = (255, 255, 255) if o.is_player else (230, 120, 90) if o.trainer_type else (220, 170, 255)
            pg.draw.circle(scr, color, (cx, cy), TILE // 2 - 2)
            vx, vy = D.DIR_VECTORS.get(D.DIR_BY_NAME.get(o.facing, 0), (0, 0))
            pg.draw.line(scr, (0, 0, 0), (cx, cy), (cx + vx * (TILE // 2 - 2), cy + vy * (TILE // 2 - 2)), 3)
            if not o.is_player:
                scr.blit(self.small.render(str(o.local_id), True, (255, 255, 255)), (cx - 4, cy - TILE + 2))

    def draw_panel(self, gba: np.ndarray | None) -> None:
        pg, scr = self.pg, self.screen
        snap = self.host.snapshot
        x0 = PANEL_X
        if gba is not None:
            surf = pg.surfarray.make_surface(np.ascontiguousarray(gba.swapaxes(0, 1)))
            scr.blit(pg.transform.scale(surf, (240 * GBA_ZOOM, 160 * GBA_ZOOM)), (x0, 0))
        y = 160 * GBA_ZOOM + 8
        mc = MODE_COLORS[snap.mode]

        def line(text, color=(220, 220, 220), font=None):
            nonlocal y
            scr.blit((font or self.font).render(text, True, color), (x0, y))
            y += 18 if font is None else 15

        line(f"mode {snap.mode.value.upper()}   screen {snap.screen}", mc)
        p = snap.player
        line(f"map {snap.map_name} ({snap.map_group}.{snap.map_num})   frame {snap.frame}" + ("   [4x]" if self.fast else ""))
        if p:
            line(f"player ({p.x},{p.y}) elev {p.elevation} facing {p.facing}"
                 f"{' moving' if p.moving else ''}{' surf' if p.surfing else ''}{' bike' if p.on_bike else ''}"
                 f"   on {self.d.mb_names.get(p.behavior, p.behavior)}")
        cur = self.host.controller.current
        line("intent: " + (f"#{cur.id} {cur.kind} {cur.args}" if cur else "-"), (240, 220, 80))
        for it in list(self.intents)[:3]:
            if it.finished:
                line(f"  #{it.id} {it.kind} -> {it.status.value} {it.reason}", (170, 170, 140), self.small)
        if snap.text:
            for i, t in enumerate(snap.text.shown.splitlines()[-3:]):
                line(("text: " if i == 0 else "      ") + t, (255, 255, 255))
        if snap.menu:
            line(f"menu {snap.menu.kind}: cursor {snap.menu.cursor} of {snap.menu.min}..{snap.menu.max}"
                 "   (press 0-9 to choose)", (255, 200, 120))
        if snap.battle:
            line(f"battle flags {snap.battle.type_flags:#x} action cursor {snap.battle.action_cursor}", (255, 160, 220))
        y += 4
        for e in self.events:
            line(str(e).replace("\n", " / ")[:90], (150, 180, 200), self.small)

    def draw(self) -> None:
        pg, scr = self.pg, self.screen
        scr.fill((0, 0, 0))
        gba = self.gba_image()
        snap = self.host.snapshot
        if snap.mode == Mode.SCREEN and gba is not None:
            # fallback: the GBA screen is the main view, input goes straight to the game
            z = min(MAP_W * TILE // 240, MAP_H * TILE // 160)
            surf = pg.surfarray.make_surface(np.ascontiguousarray(gba.swapaxes(0, 1)))
            w, h = 240 * z, 160 * z
            ox, oy = (MAP_W * TILE - w) // 2, (MAP_H * TILE - h) // 2
            scr.blit(pg.transform.scale(surf, (w, h)), (ox, oy))
            pg.draw.rect(scr, MODE_COLORS[Mode.SCREEN], (ox - 2, oy - 2, w + 4, h + 4), 2)
            scr.blit(self.font.render(f"SCREEN fallback: {snap.screen} (raw input)", True,
                                      MODE_COLORS[Mode.SCREEN]), (ox, oy + h + 6))
        else:
            self.draw_map()
        self.draw_panel(gba)
        pg.display.flip()

    # -- main loop ------------------------------------------------------------
    def run(self, max_frames: int | None = None, screenshot: str | None = None) -> int:
        pg = self.pg
        clock = pg.time.Clock()
        frames = 0
        running = True
        while running:
            for ev in pg.event.get():
                if not self.handle(ev):
                    running = False
            raw = self.raw_keys()
            for _ in range(4 if self.fast else 1):
                r = self.host.step(raw)
                frames += 1
                for e in r.events:
                    if e.kind != "player_step":
                        self.events.appendleft(str(e))
            self.draw()
            if max_frames and frames >= max_frames:
                running = False
            clock.tick(60)
        if screenshot:
            pg.image.save(self.screen, screenshot)
        pg.quit()
        return 0


def run(host: EmeraldHost, pack=None, state_path=None, max_frames=None, screenshot=None, script=None) -> int:
    view = DebugView(host, pack, state_path)
    if script:
        script(view)
    return view.run(max_frames, screenshot)
