"""Image-as-document estimator.

A single image input counts as 1 page. Token side is the generated
description, which is more detailed when the image IS the input document.

Bands calibrated 2026-05-19 against 72 image descriptions across 21
production .poma archives:

  Embedded images — observed
    p50  =  48
    p90  = 245
    p99  = 296
    max  = 342

  Image-as-document — no direct measurement in the
  archive corpus, all of those are PDFs. Bands extrapolated from the
  "short" data scaled up (image-as-document descriptions are longer) and
  widened on the high end for complex single-image inputs (diagrams,
  screenshots, charts) that get a thorough description.
"""

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from poma_estimate.credit_math import CreditBand, band_for

IMAGE_EXTS = {
    "png", "jpg", "jpeg", "jpe", "gif", "bmp", "heic", "heif",
    "svg", "ico", "tiff", "tif", "webp",
    # Vector / raw-photo formats that need a format conversion. We can't
    # parse them locally but file presence + size still drives a band.
    "psd", "ai", "eps",
}

# Image-as-document — "detailed" description. Bands cover
# everything from a casual photo (low) to a dense diagram (high).
DETAILED_TOKENS_LOW = 100
DETAILED_TOKENS_EXPECTED = 400
DETAILED_TOKENS_HIGH = 1500


_PAGELIKE_BYTES = 400_000
_PAGELIKE_SIDE = 1200


def _dimensions(path: Path) -> tuple[int, int] | None:
    return (_dims_frames(path) or (None, 1))[0]


def _dims_frames(path: Path) -> tuple[tuple[int, int], int, str] | None:
    """(width, height), frame count, decoded format. Multi-frame TIFF/GIF files carry one page or image per frame."""
    try:
        with Image.open(path) as im:
            size = im.size
            fmt = (im.format or "").lower()
            try:
                frames = max(1, int(getattr(im, "n_frames", 1) or 1))
            except Exception:  # damaged multi-frame container: dimensions are fine, frame table is not
                frames = 1
            return size, frames, fmt
    except (UnidentifiedImageError, OSError):
        return None


def estimate_image(path: Path) -> tuple[CreditBand, dict]:
    df = _dims_frames(path)
    dims, n_frames, fmt = (df if df else (None, 1, ""))
    size_bytes = path.stat().st_size
    # Tiny icon-ish images (<100px on either side) get very short descriptions.
    if (dims is not None and min(dims) < 100) or size_bytes < 5_000:
        low, expected, high = 20, 60, 250
        complexity = "tiny"
    elif size_bytes < 20_000:
        low, expected, high = 30, 100, 400
        complexity = "small"
    elif size_bytes >= _PAGELIKE_BYTES or (dims is not None and min(dims) >= _PAGELIKE_SIDE):
        # Large photos are usually scanned pages or screenshots whose description
        # carries the page text, so the band leans high (about 2 units each).
        low, expected, high = 280, 1100, 3000
        complexity = "page-like"
    else:
        low, expected, high = (
            DETAILED_TOKENS_LOW,
            DETAILED_TOKENS_EXPECTED,
            DETAILED_TOKENS_HIGH,
        )
        complexity = "normal"

    pages = 1
    if (fmt == "tiff" or (not fmt and path.suffix.lower() in (".tif", ".tiff"))) and n_frames > 1:
        # a multi-page TIFF is converted to a PDF with one page per frame: bill each frame as a page
        pages = n_frames
        low, expected, high = low * n_frames, expected * n_frames, high * n_frames
    band = band_for(pages=pages, tokens_low=low, tokens_expected=expected, tokens_high=high)
    detail = {
        "kind": "image",
        "dimensions": dims,
        "size_bytes": size_bytes,
        "complexity": complexity,
        "n_frames": n_frames,  # GIF: at most 6 frames are counted; frames do not add pages
        "image_format": fmt or None,
        "tokens_low": low,
        "tokens_expected": expected,
        "tokens_high": high,
        "pages": pages,
    }
    return band, detail
