"""Canonical POMA unit math.

units = max(pages, ceil(tokens / 1000)) per file. Kept as a tiny pure module
so it can be checked by hand.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CreditBand:
    """Credit estimate as a (low, expected, high) band.

    Bands collapse to a single number for fully-deterministic inputs
    (PDF page count + extracted text) and widen for inputs with vision
    description uncertainty.
    """

    low: int
    expected: int
    high: int

    def __add__(self, other: "CreditBand") -> "CreditBand":
        return CreditBand(
            self.low + other.low,
            self.expected + other.expected,
            self.high + other.high,
        )

    def is_point(self) -> bool:
        return self.low == self.expected == self.high


def credits_for(pages: int, tokens: int) -> int:
    """Single-point unit count: max(pages, 1,000-token blocks)."""
    if pages < 0 or tokens < 0:
        raise ValueError("pages and tokens must be non-negative")
    token_blocks = (tokens + 999) // 1000
    return max(pages, token_blocks)


def band_for(
    pages: int,
    tokens_low: int,
    tokens_expected: int,
    tokens_high: int,
) -> CreditBand:
    return CreditBand(
        low=credits_for(pages, tokens_low),
        expected=credits_for(pages, tokens_expected),
        high=credits_for(pages, tokens_high),
    )


def round_estimate(n: int) -> int:
    """Display rounding: <10 exact, <100 nearest 10, <1k nearest 50,
    <10k nearest 100, <100k nearest 1,000, else nearest 10,000
    (within about 5% from 100 upwards; below that the step can be up to 20%)."""
    if n < 10:
        return n
    for limit, step in ((100, 10), (1_000, 50), (10_000, 100), (100_000, 1_000)):
        if n < limit:
            return _round_to(n, step)
    return _round_to(n, 10_000)


def _round_to(n: int, step: int) -> int:
    return ((n + step // 2) // step) * step


# File-size corner: KB-per-unit constants calibrated from sample
# conversion runs. Used as ONE corner of a bounding-box
# estimate alongside page count and text-token count — never as the
# sole input, because filesize alone is wildly misleading for
# text-dense formats (a 300KB academic PDF can be 50 credits; a 30MB
# image-heavy newsletter the same).
#
# Calibration data sources:
#   PDF        : text PDFs vary widely; conservative ~30 KB per unit
#   DOCX       : derived from sample .docx files — text content dominates
#   PPTX       : dominated by embedded image bytes; 1 credit per slide,
#                and slides are typically 1-5 MB each
#   XLSX       : sheet bytes are mostly XML padding; text density wins
KB_PER_CREDIT = {
    "pdf":  30,    # conservative — favors over-estimation
    "docx": 100,
    "pptx": 3000,  # slide-count side dominates; filesize loose
    "xlsx": 50,
    "html": 5,
    "md":   3,
    "txt":  3,
    "rtf":  3,
    "epub": 5,
}


def filesize_corner(size_bytes: int, ext: str) -> int:
    """File-size-based credit floor for an extension we have a
    calibrated bytes-per-credit constant for. Returns 0 if no constant
    is known (caller should ignore this corner).
    """
    kb_per = KB_PER_CREDIT.get(ext)
    if not kb_per:
        return 0
    kb = max(1, size_bytes // 1024)
    return max(1, kb // kb_per)


def corner_max(*corners: int) -> int:
    """Pick the largest of several independent credit estimates.

    Credits = max(pages, ceil(tokens/1000)) is already a max; adding a
    filesize-corner just extends that to a third signal. Most
    conservative (and therefore safest customer-side) wins.
    """
    return max((c for c in corners if c is not None and c >= 0), default=0)


# Fast-mode token overhead per type. Measured against full-mode on a
# 178-file sample folder (May 2026):
#   DOCX: -23% — regex misses headers/footers/footnotes/comments
#   XLSX: -17% — regex misses formatted cells, dates as ints, etc.
#   PDF : -1.3% — pdftotext is nearly complete; mostly missing image
#                  description tokens (which barely move credit count)
#   PPTX, HTML, MD/TXT, RTF, EPUB, images : 0% (regex parity)
#
# We multiply the TOKEN side (not the credit directly) so the page-
# count floor still wins when applicable — overhead only nudges
# credits up when the token side already dominates. Errs generous so
# fast-mode estimates never under-quote relative to full-mode.
FAST_MODE_TOKEN_OVERHEAD = {
    "pdf":  1.05,   # safety margin on top of measured 1.3%
    "docx": 1.30,   # safety margin on measured 23%
    "dotx": 1.30,
    "xlsx": 1.25,   # safety margin on measured 17%
    "xltx": 1.25,
    "pptx": 1.10,   # measured parity, modest safety for speaker notes
    "potx": 1.10,
    "ppsx": 1.10,
}


def apply_fast_overhead(tokens: int, ext: str) -> int:
    """Bump a fast-mode token count by the calibrated per-type overhead."""
    mult = FAST_MODE_TOKEN_OVERHEAD.get(ext, 1.0)
    return int(tokens * mult)
