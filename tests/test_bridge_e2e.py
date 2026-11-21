"""End-to-end: run the real Lua bridge inside libmgba (headless) and check that
the state rebuilt from the diff stream is byte-identical to full captures.

Needs harness/shadow-run (make -C harness) and testrom/shadowtest.gba.
"""
import os
import socket
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from shadow.protocol import HEADER, _recv_exact, apply_message, load_capture, parse_header
from shadow.state import GBAState

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "harness" / "shadow-run"
ROM = ROOT / "testrom" / "shadowtest.gba"
BRIDGE = ROOT / "lua" / "shadow_bridge.lua"

pytestmark = pytest.mark.skipif(not (HARNESS.exists() and ROM.exists()), reason="build harness/ and testrom/ first")


def test_stream_reconstructs_state(tmp_path):
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    snapshots: dict[int, GBAState] = {}
    errors = []

    def serve():
        try:
            conn, _ = server.accept()
            st = GBAState()
            with conn:
                while True:
                    try:
                        header = _recv_exact(conn, HEADER.size)
                    except ConnectionError:
                        return
                    payload = _recv_exact(conn, parse_header(header)[3])
                    apply_message(st, header, payload)
                    snapshots[st.frame] = st.copy()
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    env = dict(os.environ, SHADOW_PORT=str(port), SHADOW_CAPTURE_DIR=str(tmp_path),
               SHADOW_CAPTURE_EVERY="97", SHADOW_CAPTURE_COUNT="0", SHADOW_CAPTURE_START="5")
    proc = subprocess.run([str(HARNESS), str(ROM), str(BRIDGE), "1500"], env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    t.join(timeout=10)
    server.close()
    assert not errors

    captures = sorted(tmp_path.glob("*.gbsh"))
    assert len(captures) >= 10
    compared = 0
    for cap in captures:
        ref = load_capture(cap)
        got = snapshots.get(ref.frame)
        if got is None:  # bridge may drop frames when the reader lags
            continue
        for name in ("io", "palette", "vram", "oam"):
            n = len(getattr(ref, name)) if name != "io" else 0x60
            np.testing.assert_array_equal(getattr(got, name)[:n], getattr(ref, name)[:n], err_msg=f"{name} @ {ref.frame}")
        compared += 1
    assert compared >= len(captures) // 2
    assert len(snapshots) > 100


def _run_with_adapter_server(delay: int, frames: int = 600) -> tuple[str | None, int, int]:
    """Run the test ROM with a renderer that answers hello with the adapter's watch list.
    Returns (game code, frames where entity matches OAM, frames where it does not)."""
    from shadow.entities import extract_world
    from shadow.games import find_adapter
    from shadow.protocol import FLAG_HELLO, encode_watch, parse_hello, read_message

    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    result = {"code": None, "ok": 0, "bad": 0}

    def serve():
        conn, _ = server.accept()
        st, adapter = GBAState(), None
        with conn:
            while True:
                try:
                    header, payload = read_message(conn)
                except ConnectionError:
                    return
                if parse_header(header)[0] & FLAG_HELLO:
                    result["code"] = parse_hello(payload)[0]
                    adapter = find_adapter(result["code"])
                    conn.sendall(encode_watch(adapter.ram_ranges, delay))
                    continue
                apply_message(st, header, payload)
                world = extract_world(adapter, st)
                if not world.entities or st.dispcnt & 0x80:
                    continue
                e = world.entities[0]
                oam_x = int(st.oam16[e.oam[0] * 4 + 1]) & 511
                result["ok" if oam_x == (int(e.screen[0] - e.size[0] / 2) & 511) else "bad"] += 1

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    proc = subprocess.run([str(HARNESS), str(ROM), str(BRIDGE), str(frames)],
                          env=dict(os.environ, SHADOW_PORT=str(port)), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    t.join(timeout=10)
    server.close()
    return result["code"], result["ok"], result["bad"]


def test_hello_watch_and_ram_delay():
    # The test ROM (like Pokemon) runs logic, then uploads OAM in the next VBlank, so RAM read at
    # VBlank is one frame ahead of the picture. With a one-frame delay they must agree exactly.
    code, ok, bad = _run_with_adapter_server(delay=1)
    assert code == "STST"
    assert ok > 200 and bad == 0
    _, ok0, bad0 = _run_with_adapter_server(delay=0)
    assert bad0 > ok0                         # without the delay the entity runs ahead


def test_capture_includes_watched_ram(tmp_path):
    from shadow.entities import extract_world
    from shadow.games import find_adapter

    env = dict(os.environ, SHADOW_STREAM="0", SHADOW_CAPTURE_DIR=str(tmp_path), SHADOW_CAPTURE_EVERY="50",
               SHADOW_CAPTURE_COUNT="0", SHADOW_WATCH="0x02000000:12", SHADOW_RAM_DELAY="1")
    proc = subprocess.run([str(HARNESS), str(ROM), str(BRIDGE), "700"], env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr
    checked = 0
    for cap in sorted(tmp_path.glob("*.gbsh")):
        st = load_capture(cap)
        world = extract_world(find_adapter("STST"), st)
        if not world.entities or st.dispcnt & 0x80:
            continue
        e = world.entities[0]
        assert int(st.oam16[e.oam[0] * 4 + 1]) & 511 == int(e.screen[0] - 16) & 511
        checked += 1
    assert checked >= 5
