"""gba-shadow command line.

  python -m shadow view   [--port N] [--scale S] [--pack DIR] [--dump DIR [--dump-tiles]]
  python -m shadow verify CAPTURE_DIR
  python -m shadow render STATE.gbsh -o out.png [--scale S] [--pack DIR]
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

    c = sub.add_parser("verify", help="check the renderer against captured mGBA screenshots")
    c.add_argument("capture_dir")

    r = sub.add_parser("render", help="render one captured state to a PNG")
    r.add_argument("state")
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--scale", type=int, default=None)
    r.add_argument("--pack")

    args = ap.parse_args(argv)

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
