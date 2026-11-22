"""Screenshots of the web client, for checking models and 3D maps without a ROM.

    # A map from the decomp fixture (see tools/emerald_fixture.py), 3D view, plus two turned views:
    python tools/screenshot.py LittlerootTown_BrendansHouse_1F --player 5,4 --orbit 0.6,-0.25 --orbit -0.55,-0.35

    # The same for the 2D picture view:
    python tools/screenshot.py LittlerootTown_BrendansHouse_2F --view 2d

    # A server that is already running (e.g. ./start.py with a real game or the test ROM):
    python tools/screenshot.py --url http://127.0.0.1:8765

Writes build/screenshots/<name>_<view>[_<n>].png plus <name>_montage.png (all shots side by side).

How it works: the fixture state is served by the real FrameServer (shadow/server.py) through a
stub session, so the page gets exactly what a live game would send. The page is the real
client (shadow/web/index.html) in headless Chromium via Node Playwright (`npm i -g playwright`;
Chromium must be installed for it). three.js normally comes from jsDelivr; when that host isn't
reachable (as in Claude's cloud sandbox) it is downloaded once from the npm registry into
build/three/ and served to the page locally. Turned views set window.gbaShadow.orbit.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

THREE_VERSION = "0.160.0"
THREE_DIR = REPO / "build" / "three"


def three_route_dir() -> Path | None:
    """None if jsDelivr works; otherwise a local copy of the three package from npm."""
    try:
        urllib.request.urlopen(f"https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/package.json", timeout=5)
        return None
    except Exception:
        pass
    pkg = THREE_DIR / "package"
    if not (pkg / "build" / "three.module.js").exists():
        print("[screenshot] jsDelivr unreachable; fetching three.js from the npm registry")
        data = urllib.request.urlopen(f"https://registry.npmjs.org/three/-/three-{THREE_VERSION}.tgz").read()
        THREE_DIR.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = [m for m in tar.getmembers()
                       if m.name.startswith(("package/build/", "package/examples/jsm/"))]
            tar.extractall(THREE_DIR, members=members, filter="data")
    return pkg


def serve_fixture(layout: str, player, facing: int) -> int:
    """Run the real FrameServer on a fixture in a background thread; returns its HTTP port."""
    from emerald_fixture import build
    from shadow.entities import extract_world
    from shadow.games import pokeemerald as pe
    from shadow.server import FrameServer

    st, rom, _ = build(layout, player, facing)

    class StubSession:
        """Looks like LiveSession: the fixture state as a live game at 60 frames per second."""
        adapter, port = pe.ADAPTER, 0

        def __init__(self):
            self.rom, self.seq = rom, 0

        def latest(self):
            time.sleep(1 / 60)
            self.seq += 1
            return st, self.seq

        def world(self, s):
            return extract_world(self.adapter, s, "BPEE")

    server = FrameServer(StubSession(), http_port=0)
    ready = asyncio.Event()
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(server.run(ready)), daemon=True).start()
    deadline = time.time() + 10
    while not ready.is_set() and time.time() < deadline:
        time.sleep(0.05)
    return server.http_port


JS = r"""
const { chromium } = require('playwright');
const cfg = JSON.parse(process.env.SHADOW_SHOT_CONFIG);
(async () => {
  const b = await chromium.launch({ args: ['--use-gl=swiftshader', '--enable-unsafe-swiftshader'] });
  const p = await b.newPage({ viewport: { width: cfg.width, height: cfg.height } });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  if (cfg.three) {
    await p.route('https://cdn.jsdelivr.net/npm/three@' + cfg.version + '/**', r => {
      const rel = r.request().url().split('three@' + cfg.version + '/')[1];
      r.fulfill({ path: cfg.three + '/' + rel, contentType: 'text/javascript' });
    });
  }
  await p.goto(cfg.url);
  await p.evaluate((v) => {
    document.querySelector('details').open = false;
    const radio = document.querySelector('input[name=view][value="' + v + '"]');
    if (radio && !radio.checked) radio.click();
  }, cfg.view);
  await p.waitForTimeout(cfg.wait);
  const shots = [];
  for (let i = 0; i < cfg.orbits.length; i++) {
    const [yaw, pitch] = cfg.orbits[i];
    await p.evaluate(([y, pt]) => { const o = window.gbaShadow.orbit; o.hold = true; o.yaw = y; o.pitch = pt; }, [yaw, pitch]);
    await p.waitForTimeout(500);
    await p.screenshot({ path: cfg.paths[i] });
    shots.push(cfg.paths[i]);
  }
  const status = await p.textContent('#status');
  console.log(JSON.stringify({ shots, status, view: await p.evaluate(() => window.gbaShadow.view), errors }));
  await b.close();
})();
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("layout", nargs="?", help="decomp layout name (omit with --url)")
    ap.add_argument("--url", help="screenshot an already running server instead of a fixture")
    ap.add_argument("--player", default="5,4", help="player tile x,y for the fixture")
    ap.add_argument("--facing", type=int, default=1, help="1 down, 2 up, 3 left, 4 right")
    ap.add_argument("--view", choices=("3d", "2d"), default="3d")
    ap.add_argument("--orbit", action="append", default=[], metavar="YAW,PITCH",
                    help="extra turned view in radians (repeatable); the game's view is always shot first")
    ap.add_argument("--size", default="960x640", help="viewport size")
    ap.add_argument("--wait", type=float, default=3.0, help="seconds to let models load")
    ap.add_argument("--out", default=str(REPO / "build" / "screenshots"))
    args = ap.parse_args()
    if not args.layout and not args.url:
        ap.error("give a layout or --url")
    node = shutil.which("node")
    if not node:
        sys.exit("needs Node.js with Playwright (npm i -g playwright && npx playwright install chromium)")

    url = args.url
    if not url:
        px, py = map(int, args.player.split(","))
        url = f"http://127.0.0.1:{serve_fixture(args.layout, (px, py), args.facing)}/"
    name = args.layout or "live"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    orbits = [(0.0, 0.0)] + [tuple(map(float, o.split(","))) for o in args.orbit]
    paths = [str(out / f"{name}_{args.view}{'' if i == 0 else f'_{i}'}.png") for i in range(len(orbits))]
    w, h = map(int, args.size.split("x"))
    three = three_route_dir()
    cfg = {"url": url, "view": args.view, "orbits": orbits, "paths": paths, "width": w, "height": h,
           "wait": int(args.wait * 1000), "three": str(three) if three else None, "version": THREE_VERSION}
    npm_root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    proc = subprocess.run([node, "-e", JS], capture_output=True, text=True, timeout=300,
                          env={**os.environ, "NODE_PATH": npm_root, "SHADOW_SHOT_CONFIG": json.dumps(cfg)})
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    if proc.returncode != 0 or not lines:
        sys.exit(f"screenshot failed:\n{proc.stderr[-2000:]}")
    result = json.loads(lines[-1])
    print(f"[screenshot] {result['status']}  (view: {result['view']})")
    for e in result["errors"]:
        print(f"[screenshot] page error: {e}")

    from PIL import Image
    ims = [Image.open(p).convert("RGB") for p in result["shots"]]
    montage = Image.new("RGB", (sum(i.width for i in ims) + 10 * (len(ims) - 1), ims[0].height), (11, 12, 16))
    x = 0
    for im in ims:
        montage.paste(im, (x, 0))
        x += im.width + 10
    montage_path = out / f"{name}_montage.png"
    montage.save(montage_path)
    for p in result["shots"] + [str(montage_path)]:
        print(f"[screenshot] wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
