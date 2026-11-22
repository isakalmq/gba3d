#!/usr/bin/env python3
"""Start everything: the gba-shadow renderer, an emulator running the bridge, and the browser.

    ./start.py                      # the only ROM in roms/, 3D web client
    ./start.py emerald              # a ROM in roms/ whose file name contains "emerald"
    ./start.py --view               # pygame viewer instead of the web client
    ./start.py --dump dumps/mygame  # viewer, saving every new sprite (for making a pack)
    ./start.py --pack packs/mygame  # viewer with replacement graphics
    ./start.py --emulator mgba      # use mGBA's own app (load the script by hand, see below)

Emulators:
  player  player/shadow-play: a small libmgba-based player that loads the bridge
          script at startup. Built automatically on first use (needs
          libmgba-dev and libsdl2-dev).
  mgba    the mGBA app. mGBA 0.10 cannot load scripts from the command line, so
          this prints which script to load via Tools > Scripting.
  auto    player if it can be built, else mgba (default).
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parent
ROMS = REPO / "roms"
BRIDGE = REPO / "lua" / "shadow_bridge.lua"
PLAYER = REPO / "player" / "shadow-play"
ROM_SUFFIXES = (".gba", ".zip", ".7z")


def find_rom(query: str | None) -> Path:
    if query and Path(query).is_file():
        return Path(query).resolve()
    roms = sorted(p for p in ROMS.iterdir() if p.suffix.lower() in ROM_SUFFIXES) if ROMS.is_dir() else []
    if query:
        roms = [p for p in roms if query.lower() in p.name.lower()]
    if len(roms) == 1:
        return roms[0]
    if not roms:
        what = f"matching {query!r} " if query else ""
        sys.exit(f"No ROM {what}found in {ROMS}/ - put your .gba file there (see roms/README.md).")
    names = "\n  ".join(p.name for p in roms)
    sys.exit(f"Several ROMs in {ROMS}/ - pick one by (part of) its name:\n  {names}")


def build_player() -> bool:
    if PLAYER.exists() and PLAYER.stat().st_mtime >= (REPO / "player" / "shadow_play.c").stat().st_mtime:
        return True
    if not shutil.which("sdl2-config") or not shutil.which("cc"):
        print("[start] can't build player/shadow-play (needs a C compiler, libmgba-dev and libsdl2-dev)")
        return False
    print("[start] building player/shadow-play ...")
    result = subprocess.run(["make", "-C", str(REPO / "player")], capture_output=True, text=True)
    if result.returncode != 0:
        print("[start] build failed:\n" + result.stderr.strip())
        return False
    return True


def find_mgba() -> str | None:
    for name in ("mgba-qt", "mgba", "/usr/games/mgba-qt"):
        path = shutil.which(name) or (name if Path(name).exists() else None)
        if path:
            return path
    return None


def wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit("[start] the renderer exited during startup (is the port already in use?)")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    sys.exit(f"[start] the renderer did not start listening on port {port}")


def stop(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", nargs="?", help="ROM file, or part of a file name in roms/")
    ap.add_argument("--view", action="store_true", help="pygame viewer instead of the web client")
    ap.add_argument("--emulator", choices=("auto", "player", "mgba"), default="auto")
    ap.add_argument("--port", type=int, default=61337, help="bridge port")
    ap.add_argument("--http-port", type=int, default=8765, help="web client port")
    ap.add_argument("--no-browser", action="store_true", help="don't open the web client")
    ap.add_argument("--scale", type=int, default=3, help="player window scale")
    ap.add_argument("--pack", help="replacement pack (implies --view)")
    ap.add_argument("--dump", help="dump new sprites into this directory (implies --view)")
    args = ap.parse_args()

    rom = find_rom(args.rom)
    use_player = args.emulator == "player" or (args.emulator == "auto" and build_player())
    if args.emulator == "player" and not build_player():
        return 1
    mgba = None if use_player else find_mgba()
    if not use_player and not mgba:
        sys.exit("[start] no emulator: build player/ (see README) or install mGBA 0.10+ (sudo apt install mgba-qt)")

    args.view = args.view or bool(args.pack or args.dump)
    if args.view:
        cmd = [sys.executable, "-m", "shadow", "view", "--port", str(args.port)]
        if args.pack:
            cmd += ["--pack", str(Path(args.pack).resolve())]
        if args.dump:
            cmd += ["--dump", str(Path(args.dump).resolve())]
    else:
        cmd = [sys.executable, "-m", "shadow", "serve", "--port", str(args.port), "--http-port", str(args.http_port),
               "--rom", str(rom)]
    renderer = subprocess.Popen(cmd, cwd=REPO)
    emulator = None
    try:
        wait_for_port(args.port, renderer)
        env = dict(os.environ, SHADOW_PORT=str(args.port))
        print(f"[start] ROM: {rom.name}")
        if use_player:
            emulator = subprocess.Popen([str(PLAYER), str(rom), str(BRIDGE), "--scale", str(args.scale)], env=env)
        else:
            emulator = subprocess.Popen([mgba, str(rom)], env=env)
            print("\n[start] mGBA can't load scripts from the command line. In mGBA choose\n"
                  "        Tools > Scripting... > File > Load script and pick:\n"
                  f"        {BRIDGE}\n")
        if not args.view and not args.no_browser:
            url = f"http://127.0.0.1:{args.http_port}"
            print(f"[start] opening {url}")
            webbrowser.open(url)
        print("[start] close the emulator window (or press Ctrl+C) to stop everything")
        while emulator.poll() is None and renderer.poll() is None:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop(emulator)
        stop(renderer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
