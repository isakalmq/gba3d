"""The in-process core (host/libgbahost.so): stepping, memory views, savestates."""
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
ROM = ROOT / "testrom" / "shadowtest.gba"
LIB = ROOT / "host" / "libgbahost.so"

pytestmark = pytest.mark.skipif(not LIB.exists(), reason="build host/libgbahost.so with `make -C host`")


def test_display_state_matches_mgba_output():
    from shadow.core import Core
    from shadow.render import Renderer
    renderer = Renderer(scale=1)
    with Core(ROM) as core:
        checked = 0
        for f in range(1200):
            core.run_frame()
            if f % 97 == 0:
                ours = renderer.render(core.display_state()).image
                assert np.array_equal(ours, core.video_rgb()), f"frame {f}"
                checked += 1
        assert checked >= 10


def test_memory_views_and_savestates():
    from shadow.core import Core
    with Core(ROM) as core:
        core.step(frames=30)
        assert core.read(0x080000A0, 4) == ROM.read_bytes()[0xA0:0xA4]
        state = core.save_state()
        frame = core.frame
        vram = core.read(0x06000000, 0x800)
        core.step(frames=200)
        core.load_state(state)
        assert core.frame == frame
        assert core.read(0x06000000, 0x800) == vram
