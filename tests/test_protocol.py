import socket
import struct
import time

import numpy as np
import pytest

from shadow.protocol import HEADER, FrameReceiver, ProtocolError, apply_message
from shadow.state import REGION_IO, REGION_OAM, REGION_VRAM, GBAState


def message(frame, records, full=False):
    """Build a message exactly like lua/shadow_bridge.lua does."""
    payload = b"".join(struct.pack("<BII", r, off, len(d)) + d for r, off, d in records)
    return HEADER.pack(b"GBSH", 1, 1 if full else 0, len(records), frame, len(payload)) + payload


def split(msg):
    return msg[:HEADER.size], msg[HEADER.size:]


def test_apply_full_then_diff():
    st = GBAState()
    vram = bytes(range(256)) * (0x18000 // 256)
    assert apply_message(st, *split(message(7, [(REGION_VRAM, 0, vram), (REGION_IO, 0, b"\x03\x10")], full=True)))
    assert st.frame == 7 and st.dispcnt == 0x1003
    assert apply_message(st, *split(message(8, [(REGION_VRAM, 0x400, b"\xff" * 4)]))) is False
    assert st.frame == 8
    # 0x3FF keeps its old value (0xFF), 0x400-0x403 were overwritten, 0x404 is untouched (4).
    assert bytes(st.vram[0x3FF:0x405]) == b"\xff" * 5 + b"\x04"


def test_rejects_bad_input():
    st = GBAState()
    with pytest.raises(ProtocolError):
        apply_message(st, *split(message(1, [(REGION_OAM, 0x3FF, b"ab")])))
    bad = bytearray(message(1, []))
    bad[0:4] = b"NOPE"
    with pytest.raises(ProtocolError):
        apply_message(st, *split(bytes(bad)))


def test_receiver_over_tcp():
    rx = FrameReceiver(port=0).start()
    try:
        with socket.create_connection(("127.0.0.1", rx.port)) as c:
            c.sendall(message(1, [(REGION_IO, 0, b"\x00\x11")], full=True))
            c.sendall(message(2, [(REGION_OAM, 8, b"\x12\x34")]))
            deadline = time.time() + 5
            while time.time() < deadline:
                st, seq = rx.latest()
                if seq >= 2:
                    break
                time.sleep(0.01)
        assert st is not None and st.frame == 2
        assert st.dispcnt == 0x1100
        assert int(st.oam16[4]) == 0x3412
        assert np.count_nonzero(st.vram) == 0
    finally:
        rx.stop()
