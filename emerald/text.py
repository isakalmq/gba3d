"""Decode Gen 3 encoded strings (charmap.txt) to Python text."""
from __future__ import annotations

from .data import EmeraldData

CHAR_PROMPT_SCROLL, CHAR_PROMPT_CLEAR = 0xFA, 0xFB
EXT_CTRL_CODE_BEGIN, PLACEHOLDER_BEGIN, CHAR_NEWLINE, EOS = 0xFC, 0xFD, 0xFE, 0xFF
CHAR_KEYPAD_ICON, CHAR_EXTRA_SYMBOL, CHAR_DYNAMIC = 0xF8, 0xF9, 0xF7

# src/string_util.c GetExtCtrlCodeLength: code byte + its arguments
_EXT_LEN = [1, 2, 2, 2, 4, 2, 2, 1, 2, 1, 1, 3, 2, 2, 2, 1, 3, 2, 2, 2, 2, 1, 1, 1, 1]
EXT_ESCAPE = 0x0C


def decode(data: bytes, charmap: dict[int, str], stop: int | None = None) -> str:
    """Decode until EOS (or `stop` bytes). Paragraph breaks (wait-for-button and
    clear) become a blank line, scrolls and newlines become a newline, and
    formatting codes are dropped."""
    out: list[str] = []
    i, n = 0, len(data) if stop is None else min(stop, len(data))
    while i < n:
        b = data[i]
        if b == EOS:
            break
        if b == CHAR_NEWLINE or b == CHAR_PROMPT_SCROLL:
            out.append("\n")
        elif b == CHAR_PROMPT_CLEAR:
            out.append("\n\n")
        elif b == EXT_CTRL_CODE_BEGIN:
            code = data[i + 1] if i + 1 < len(data) else 0
            if code == EXT_ESCAPE and i + 2 < len(data):
                out.append(charmap.get(data[i + 2], ""))
            i += 1 + (_EXT_LEN[code] if code < len(_EXT_LEN) else 1)
            continue
        elif b in (PLACEHOLDER_BEGIN, CHAR_KEYPAD_ICON, CHAR_EXTRA_SYMBOL, CHAR_DYNAMIC):
            i += 2  # unexpanded placeholder / icon: skip its argument
            continue
        else:
            out.append(charmap.get(b, ""))
        i += 1
    return "".join(out)


def encode_simple(s: str, data: EmeraldData) -> bytes:
    """Encode plain text (testing helper). Unknown characters are dropped."""
    rev = {v: k for k, v in data.charmap.items()}
    out = bytearray()
    for ch in s:
        if ch == "\n":
            out.append(CHAR_NEWLINE)
        elif ch in rev:
            out.append(rev[ch])
    out.append(EOS)
    return bytes(out)
