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
