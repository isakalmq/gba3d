"""gba-shadow command line.

  python -m shadow view   [--port N] [--scale S] [--pack DIR] [--dump DIR [--dump-tiles]]
  python -m shadow serve  [--port N] [--http-port N]      (WebSocket + three.js demo client)
  python -m shadow verify CAPTURE_DIR
  python -m shadow render STATE.gbsh -o out.png [--scale S] [--pack DIR]
  python -m shadow entities STATE.gbsh --game CODE        (print the adapter's view of a capture)
"""
from __future__ import annotations

import argparse
import sys

from .protocol import DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shadow", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("view", help="open the shadow view and wait for the mGBA bridge")
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--port", type=int, default=DEFAULT_PORT)
    v.add_argument("--scale", type=int, default=None,
                   help="render resolution multiplier (default: the pack's scale, else 1)")
    v.add_argument("--zoom", type=int, default=None, help="window zoom on top of --scale (default: fit ~3x)")
    v.add_argument("--pack", help="replacement pack directory")
    v.add_argument("--dump", help="directory to dump never-seen-before sprites into")
    v.add_argument("--dump-tiles", action="store_true", help="also dump BG tiles (slower)")

    w = sub.add_parser("serve", help="publish frames + entities over WebSocket (open the printed URL)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=DEFAULT_PORT, help="port the mGBA bridge connects to")
    w.add_argument("--http-host", default="127.0.0.1")
    w.add_argument("--http-port", type=int, default=8765, help="port for the web client and /ws")
    w.add_argument("--rom", help="ROM file for static game data (default: find it in roms/ by game code)")

    e = sub.add_parser("entities", help="print entities extracted from a capture (needs RAM in the capture)")
    e.add_argument("state")
    e.add_argument("--game", required=True, help="4-letter game code, e.g. BPEE")

    c = sub.add_parser("verify", help="check the renderer against captured mGBA screenshots")
    c.add_argument("capture_dir")

    r = sub.add_parser("render", help="render one captured state to a PNG")
    r.add_argument("state")
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--scale", type=int, default=None)
    r.add_argument("--pack")

    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import run_server
        return run_server(args.host, args.port, args.http_host, args.http_port, rom=args.rom)

    if args.cmd == "entities":
        import json

        from .entities import extract_world
        from .games import find_adapter
        from .protocol import load_capture
        adapter = find_adapter(args.game)
        if adapter is None:
            print(f"no adapter for game code {args.game!r}")
            return 1
        print(json.dumps(extract_world(adapter, load_capture(args.state), args.game).to_dict(), indent=2))
        return 0

    if args.cmd == "verify":
        from .verify import verify_dir
        checked, bad = verify_dir(args.capture_dir)
        print(f"\n{checked - bad}/{checked} frames match mGBA exactly")
        return 0 if checked and not bad else 1

    from .pack import Dumper, ReplacementPack

    pack = ReplacementPack(args.pack) if args.pack else None
    scale = args.scale or (pack.scale if pack else 1)

    if args.cmd == "render":
        from PIL import Image

        from .protocol import load_capture
        from .render import Renderer
        result = Renderer(scale=scale, pack=pack).render(load_capture(args.state))
        Image.fromarray(result.image).save(args.output)
        print(f"wrote {args.output} ({result.image.shape[1]}x{result.image.shape[0]}, "
              f"{sum(s.replaced for s in result.sprites)} sprites replaced, {result.tiles_replaced} tiles replaced)")
        return 0

    from .viewer import run_viewer
    dumper = Dumper(args.dump, tiles=args.dump_tiles) if args.dump else None
    return run_viewer(args.host, args.port, scale, args.zoom, pack, dumper)


if __name__ == "__main__":
    sys.exit(main())
