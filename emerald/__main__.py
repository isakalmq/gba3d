"""Emerald bridge command line.

  python -m emerald view ROM [--save SAV] [--state STATE] [--pack DIR]   debug client
  python -m emerald view --demo                                         same, against a fake game (no ROM)
  python -m emerald probe ROM [--state STATE] [--frames N]              print the snapshot as JSON
  python -m emerald boot ROM -o STATE [--save SAV]                      play through to the overworld, save a state
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _host(args):
    from .host import EmeraldHost
    host = EmeraldHost(args.rom, save=getattr(args, "save", None))
    if getattr(args, "state", None):
        host.core.load_state(Path(args.state).read_bytes())
        host.step()
    return host


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m emerald", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("view", help="top-down debug client")
    v.add_argument("rom", nargs="?")
    v.add_argument("--demo", action="store_true", help="run against the built-in fake game")
    v.add_argument("--save", help="battery save file (created if missing)")
    v.add_argument("--state", help="savestate to load at start; F5/F9 save/load it")
    v.add_argument("--pack", help="shadow replacement pack for the GBA screen")
    v.add_argument("--frames", type=int, help="quit after N frames (testing)")
    v.add_argument("--screenshot", help="save the window to this PNG on exit")

    p = sub.add_parser("probe", help="run some frames and print the snapshot")
    p.add_argument("rom")
    p.add_argument("--save")
    p.add_argument("--state")
    p.add_argument("--frames", type=int, default=1)

    b = sub.add_parser("boot", help="press through title/intro until the overworld, then save a state")
    b.add_argument("rom")
    b.add_argument("-o", "--output", required=True)
    b.add_argument("--save")
    b.add_argument("--max-frames", type=int, default=60 * 60 * 6)

    args = ap.parse_args(argv)

    if args.cmd == "view":
        from . import debugview
        pack = None
        if args.pack:
            from shadow.pack import ReplacementPack
            pack = ReplacementPack(args.pack)
        if args.demo:
            from .fake import demo
            from .host import EmeraldHost
            host, state_path = EmeraldHost(None, core=demo()), None
        else:
            if not args.rom:
                ap.error("view needs a ROM (or --demo)")
            host = _host(args)
            state_path = Path(args.state) if args.state else Path(args.rom).with_suffix(".ss1")
        return debugview.run(host, pack, state_path, args.frames, args.screenshot)

    if args.cmd == "probe":
        host = _host(args)
        for _ in range(args.frames):
            host.step()
        s = host.snapshot
        out = s.to_dict()
        out["tasks"] = list(s.tasks)
        if host.grid is not None:
            out["grid"] = {"buffer_size": [host.grid.width, host.grid.height], "map_size": host.grid.map_bounds(),
                           "connections": host.grid.connections}
        print(json.dumps(out, indent=1, default=str))
        return 0

    if args.cmd == "boot":
        from .boot import boot_to_overworld
        host = _host(args)
        ok = boot_to_overworld(host, args.max_frames, log=print)
        if not ok:
            print("did not reach the overworld; try `view` and play to it manually, then F5")
            return 1
        Path(args.output).write_bytes(host.core.save_state())
        print(f"saved {args.output} at frame {host.snapshot.frame} on {host.snapshot.map_name}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
