"""pygame window that shows the re-rendered ("shadow") view of the running game.

Keys:  B  sprite bounding boxes (green = replaced, yellow = original)
       R  toggle replacements on/off
       S  save a screenshot (shadow_XXXXXX.png in the current directory)
       Esc / Q  quit
"""
from __future__ import annotations

import os
import time

import numpy as np

from .pack import Dumper, ReplacementPack
from .protocol import FrameReceiver
from .render import Renderer
from .state import SCREEN_H, SCREEN_W


def run_viewer(host: str, port: int, scale: int, zoom: int | None,
               pack: ReplacementPack | None, dumper: Dumper | None,
               exit_after: int | None = None, save_last: str | None = None) -> int:
    import pygame

    exit_after = exit_after or int(os.environ.get("SHADOW_EXIT_AFTER", "0")) or None
    save_last = save_last or os.environ.get("SHADOW_SAVE_LAST") or None

    if zoom is None:
        zoom = max(1, round(3 / scale))
    win_w, win_h = SCREEN_W * scale * zoom, SCREEN_H * scale * zoom

    receiver = FrameReceiver(host, port).start()
    renderer = Renderer(scale=scale, pack=pack, dumper=dumper)
    print(f"[shadow] listening on {host}:{port} - load lua/shadow_bridge.lua in mGBA "
          f"(scale={scale}, pack={pack.root if pack else None}, dump={dumper.root if dumper else None})")

    pygame.display.init()
    pygame.font.init()
    screen = pygame.display.set_mode((win_w, win_h))
    font = pygame.font.Font(None, 16)
    clock = pygame.time.Clock()

    show_boxes = False
    last_seq = -1
    rendered = 0
    result = None
    frame_surface = None
    render_ms = 0.0
    title_t = 0.0

    try:
        running = True
        while running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif ev.key == pygame.K_b:
                        show_boxes = not show_boxes
                    elif ev.key == pygame.K_r:
                        renderer.replace = not renderer.replace
                        last_seq = -1
                    elif ev.key == pygame.K_s and result is not None:
                        from PIL import Image
                        name = f"shadow_{int(time.time())}.png"
                        Image.fromarray(result.image).save(name)
                        print(f"[shadow] saved {name}")

            state, seq = receiver.latest()
            if state is not None and seq != last_seq:
                last_seq = seq
                t0 = time.perf_counter()
                result = renderer.render(state)
                render_ms = 0.9 * render_ms + 0.1 * (time.perf_counter() - t0) * 1000
                rendered += 1
                surf = pygame.surfarray.make_surface(np.ascontiguousarray(result.image.swapaxes(0, 1)))
                frame_surface = pygame.transform.scale(surf, (win_w, win_h)) if zoom > 1 else surf

            screen.fill((0, 0, 0))
            if frame_surface is not None:
                screen.blit(frame_surface, (0, 0))
                if show_boxes and result is not None:
                    k = scale * zoom
                    for s in result.sprites:
                        color = (0, 255, 0) if s.replaced else (255, 220, 0)
                        if s.mode == 2:
                            color = (0, 160, 255)
                        pygame.draw.rect(screen, color, (s.x * k, s.y * k, s.width * k, s.height * k), 1)
                        screen.blit(font.render(f"{s.index}", True, color), (s.x * k + 2, s.y * k + 1))
            else:
                msg = "waiting for mGBA bridge..." if not receiver.connected else "waiting for first frame..."
                screen.blit(font.render(msg, True, (200, 200, 200)), (8, 8))
            pygame.display.flip()

            now = time.time()
            if now - title_t > 0.5:
                title_t = now
                dumped = f" dumped={dumper.count}" if dumper else ""
                repl = "on" if renderer.replace else "off"
                pygame.display.set_caption(
                    f"gba-shadow  frame={state.frame if state else '-'}  render={render_ms:.1f}ms  "
                    f"fps={clock.get_fps():.0f}  replace={repl}{dumped}")

            if exit_after and rendered >= exit_after:
                running = False
            clock.tick(240)
    finally:
        if save_last and result is not None:
            from PIL import Image
            Image.fromarray(result.image).save(save_last)
        receiver.stop()
        if dumper:
            dumper.close()
        pygame.quit()
    print(f"[shadow] rendered {rendered} frames, received {receiver.frames_received}")
    return 0
