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


def _extract_text(path: Path) -> str:
    import olefile

    if not olefile.isOleFile(str(path)):
        return ""
    chunks: list[str] = []
    with olefile.OleFileIO(str(path)) as ole:
        stream_names = [
            name for name in ole.listdir() if name and name[-1] == "WordDocument"
        ]
        if not stream_names:
            return ""
        stream = ole.openstream(stream_names[0])
        data = stream.read()

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

        # Strategy 2: decode UTF-16-LE runs (Word stores Unicode runs
        # this way). Try the whole stream; surrogateescape keeps us
        # from crashing on odd bytes.
        try:
            utf16 = data.decode("utf-16-le", errors="ignore")
            # Keep only word-character-dense substrings to drop the
            # control-byte noise we'd get from the whole stream.
            for run in utf16.split("\x00"):
                if len(run) >= 4 and any(c.isalpha() for c in run):
                    chunks.append(run)
        except Exception:
            pass

    return "\n".join(chunks)


def estimate_legacy_word(path: Path) -> tuple[CreditBand, dict]:
    text = _extract_text(path)
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
        "note": "legacy binary .doc — naive OLE stream extraction",
        "tokens_low": body_tokens,
        "tokens_expected": body_tokens,
        "tokens_high": body_tokens,
    }
