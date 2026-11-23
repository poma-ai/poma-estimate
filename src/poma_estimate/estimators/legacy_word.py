"""Legacy binary Word document (.doc).

The .doc format is OLE compound file with a `WordDocument` stream that
mixes text with formatting markers. We extract printable runs naively
— good enough for token-count estimation (no LibreOffice or antiword
subprocess needed). The platform converts .doc to PDF before
reading it, so what matters here is order-of-magnitude.
"""

import string
from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.estimators.office import (
    SHORT_TOKENS_EXPECTED,
    SHORT_TOKENS_HIGH,
    SHORT_TOKENS_LOW,
)
from poma_estimate.tokenizer import count_tokens

LEGACY_WORD_EXTS = {"doc"}

_CHARS_PER_PAGE_FALLBACK = 3000

# .doc text uses Windows-1252 by default. Anything outside printable
# ASCII + common Latin-1 ends up either as binary noise (skip) or as
# a UTF-16 LE run (handled below).
_PRINTABLE = set(string.printable) - {"\x0b", "\x0c"}


_FIELD_BEGIN, _FIELD_SEP, _FIELD_END = "\x13", "\x14", "\x15"


def _drop_field_codes(text: str) -> str:
    """Drop field instructions (between 0x13 and 0x14/0x15, nestable), keep field results."""
    out: list[str] = []
    stack: list[bool] = []  # per open field: True while still in its instruction part
    in_instr = 0  # how many open fields are still in their instruction part (keeps this linear)
    for ch in text:
        if ch == _FIELD_BEGIN:
            stack.append(True)
            in_instr += 1
        elif ch == _FIELD_SEP and stack:
            if stack[-1]:
                stack[-1] = False
                in_instr -= 1
        elif ch == _FIELD_END and stack:
            in_instr -= stack.pop()
        elif not in_instr:
            out.append(ch)
    return "".join(out)


def _u32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 4], "little")


def _piece_text(wd: bytes, table: bytes) -> str | None:
    """Document text from the Word 97+ piece table (CLX in the table stream).
    Each piece is either 8-bit cp1252 ("compressed") or UTF-16-LE; reading the
    stream both ways, as the fallback does, counts the text several times.
    None when the FIB or piece table is not a consistent Word 97+ one (Word 6/95, damaged):
    every length, character position and piece must fit, and the pieces must cover the
    character counts the FIB declares. Raises ValueError for an encrypted document."""
    if len(wd) < 0x1AA or int.from_bytes(wd[0:2], "little") != 0xA5EC:
        return None
    if int.from_bytes(wd[0x0A:0x0C], "little") & 0x0100:  # fEncrypted: the text is unreadable
        raise ValueError("encrypted .doc")
    fc_clx, lcb_clx = _u32(wd, 0x1A2), _u32(wd, 0x1A6)
    clx = table[fc_clx:fc_clx + lcb_clx]
    if not lcb_clx or len(clx) != lcb_clx:
        return None
    i = 0
    while i < len(clx) and clx[i] == 0x01:  # Prc entries (formatting) precede the piece table
        i += 3 + int.from_bytes(clx[i + 1:i + 3], "little")
    if i + 5 > len(clx) or clx[i] != 0x02:
        return None
    lcb = _u32(clx, i + 1)
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    if n <= 0 or len(plc) != lcb or lcb != 4 + 12 * n:
        return None
    cps = [_u32(plc, 4 * k) for k in range(n + 1)]
    if cps[0] != 0 or any(b < a for a, b in zip(cps, cps[1:])):
        return None
    # FibRgLw97: ccpText, then footnote/header/(macro)/comment/endnote/textbox/header-textbox counts
    ccp_text = _u32(wd, 0x4C)
    ccp_all = ccp_text + sum(_u32(wd, off) for off in (0x50, 0x54, 0x5C, 0x60, 0x64, 0x68))
    # the pieces cover every declared character (+1 terminator when subdocuments exist), and real
    # pieces never overlap, so the text cannot be longer than the stream that stores it
    if not ccp_all <= cps[-1] <= ccp_all + 1 or cps[-1] > len(wd):
        return None
    parts: list[str] = []
    for k in range(n):
        count = cps[k + 1] - cps[k]
        if count == 0:
            continue
        fc = _u32(plc, 4 * (n + 1) + 8 * k + 2)
        if fc & 0x40000000:  # compressed: one cp1252 byte per character
            off, size, enc = (fc & 0x3FFFFFFF) // 2, count, "cp1252"
        else:
            off, size, enc = fc, 2 * count, "utf-16-le"
        if off + size > len(wd):
            return None
        parts.append(wd[off:off + size].decode(enc, errors="replace"))
    text = _drop_field_codes("".join(parts))
    # paragraph (\r), cell (\x07), line/page/column breaks and other control marks become whitespace
    return "".join(ch if ch >= " " or ch in "\n\t" else ("\n" if ch in "\r\x07\x0b\x0c" else " ") for ch in text)


def _extract_text(path: Path) -> tuple[str, bool]:
    """(text, exact): exact is False when the naive fallback scan was used."""
    import olefile

    if not olefile.isOleFile(str(path)):
        return "", False
    chunks: list[str] = []
    with olefile.OleFileIO(str(path)) as ole:
        stream_names = [
            name for name in ole.listdir() if name and name[-1] == "WordDocument"
        ]
        if not stream_names:
            return "", False
        data = ole.openstream(stream_names[0]).read()
        flags = int.from_bytes(data[0x0A:0x0C], "little") if len(data) >= 0x0C else 0
        tbl_name = "1Table" if flags & 0x0200 else "0Table"
        table = ole.openstream(tbl_name).read() if ole.exists(tbl_name) else b""
        text = _piece_text(data, table)
        if text is not None:
            return text, True

        # Fallback for files without a readable piece table (Word 6/95, damaged).
        # Strategy 1: pull printable ASCII runs
        ascii_run = []
        for byte in data:
            ch = chr(byte) if byte < 128 else None
            if ch and ch in _PRINTABLE:
                ascii_run.append(ch)
            else:
                if len(ascii_run) >= 4:
                    chunks.append("".join(ascii_run))
                ascii_run = []
        if len(ascii_run) >= 4:
            chunks.append("".join(ascii_run))

        # Strategy 2: UTF-16-LE runs (Word stores Unicode text this way). Keep only runs that
        # are mostly alphabetic scripts below U+0800 (Latin ... Arabic): 8-bit text and binary
        # data decoded as UTF-16 come out as CJK-range noise at ~1 token per character, and
        # counted the text of a typical .doc 5-9 times. Real CJK text is lost here (approximate).
        utf16 = data.decode("utf-16-le", errors="ignore")
        for run in utf16.split("\x00"):
            if (len(run) >= 4 and any(c.isalpha() for c in run)
                    and sum(ord(c) < 0x0800 for c in run) >= 0.9 * len(run)):
                chunks.append(run)

    text = "\n".join(chunks)
    if len(data) > 8192 and len(text) < len(data) // 100:
        # almost nothing recognisable (e.g. CJK text without a piece table): not an empty document
        raise ValueError("no readable text in .doc")
    return text, False


def estimate_legacy_word(path: Path) -> tuple[CreditBand, dict]:
    text, exact = _extract_text(path)
    body_tokens = count_tokens(text)
    pages = max(1, (len(text) + _CHARS_PER_PAGE_FALLBACK - 1) // _CHARS_PER_PAGE_FALLBACK)

    # We can't reliably count embedded images in a .doc OLE stream
    # without parsing CFB structure. Assume 0 — token count is what
    # drives the credit math here anyway, and the conservative-on-high
    # principle is preserved by the (low, expected, high) collapsing
    # to one value when we know n_images=0.
    band = band_for(
        pages=pages,
        tokens_low=body_tokens,
        tokens_expected=body_tokens,
        tokens_high=body_tokens,
    )
    return band, {
        "kind": "doc",
        "pages": pages,
        "pages_source": "char_heuristic",
        "body_tokens": body_tokens,
        "approx": not exact,
        "note": "legacy binary .doc — piece table" if exact else "legacy binary .doc — naive OLE stream scan",
        "tokens_low": body_tokens,
        "tokens_expected": body_tokens,
        "tokens_high": body_tokens,
    }
