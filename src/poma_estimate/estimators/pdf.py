"""PDF estimator.

Page count: pypdf, with the platform's oversized-page split applied.
Token side: pdfminer extraction.

For scanned PDFs (no extractable text) we don't try to OCR — we add a
per-page floor of ~850 tokens to acknowledge that OCR would produce
something. In practice the page count dominates and this floor barely
moves the credit total. For text-dense digital PDFs (legal, technical,
academic) the token side legitimately exceeds the page count.

Embedded images add description-band tokens. We count XObject /Subtype
/Image references via pypdf — fast, doesn't require rendering.
"""

import io
from pathlib import Path

import shutil
import subprocess

from pdfminer.high_level import extract_text
from pdfminer.pdfparser import PDFSyntaxError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from poma_estimate.credit_math import (
    CreditBand,
    apply_fast_overhead,
    band_for,
    corner_max,
    credits_for,
    filesize_corner,
)
from poma_estimate.estimators.office import (
    SHORT_TOKENS_EXPECTED,
    SHORT_TOKENS_HIGH,
    SHORT_TOKENS_LOW,
)
from poma_estimate.tokenizer import count_tokens

# Poppler binaries (pdfinfo / pdftotext) extract page count and text
# roughly 5-50× faster than pypdf + pdfminer.six. We sniff for them at
# import time so the choice is made once per process.
_PDFTOTEXT = shutil.which("pdftotext")

# Tokens/page assumed for scans (no local OCR): 850 expected, with a band from sparse
# single scans (280) to image-heavy pages (1,800). The page floor still binds below 1,000.
_SCANNED_PAGE_TOKEN_FLOOR = 850
_SCANNED_LOW_TOK, _SCANNED_HIGH_TOK = 280, 1800

# Pages taller than 1000pt are split into 2000pt chunks before they are counted,
# as the platform does; page counts reflect that.
_PRESPLIT_THRESHOLD_PT = 1000
_PRESPLIT_TARGET_HEIGHT_PT = 2000


def _count_pages(data: bytes) -> tuple[int, int]:
    """Return (raw_pages, presplit_adjusted_pages).

    `raw_pages` is what pypdf reports verbatim. `presplit_adjusted_pages`
    expands oversized pages (height > 1000pt) into the chunks the platform
    would actually bill. For most documents the two are equal.
    """
    splits = _page_splits(data)
    return len(splits), sum(splits)


def _page_splits(data: bytes) -> list[int]:
    """Per source page, how many billed pages it becomes after the oversized-page presplit."""
    import math

    reader = PdfReader(io.BytesIO(data))
    out = []
    for page in reader.pages:
        h = float(page.mediabox.height)
        out.append(math.ceil(h / _PRESPLIT_TARGET_HEIGHT_PT) if h > _PRESPLIT_THRESHOLD_PT else 1)
    return out


SIGNIFICANT_PAGE_FRACTION = 0.05   # figures covering >= 5 % of the page get described
SIGNIFICANT_MIN_PIXELS = 10_000    # and embedded rasters of >= 100 x 100 px ...
SIGNIFICANT_MIN_BYTES = 5_120      # ... and >= 5 KB (the platform's rule)
_SIG_MAX_PAGES = 400               # content-stream walk is sampled beyond this many pages


def _significant_images(data: bytes) -> tuple[int, int]:
    """(images drawn at >= 5 % of their page's area, image placements of >= 100x100 px and >= 5 KB).
    Walks each page's content stream tracking the transformation matrix, so an image counts where and
    as large as it is drawn; images inside form XObjects are followed two levels deep. Pages beyond
    _SIG_MAX_PAGES are extrapolated from the walked ones."""
    from pypdf.generic import ContentStream

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception:
        return 0, 0
    if not pages:
        return 0, 0
    if len(pages) > _SIG_MAX_PAGES:  # random sample, not a stride (a stride aliases with alternating layouts)
        import random
        walked = [pages[i] for i in sorted(random.Random(len(pages)).sample(range(len(pages)), _SIG_MAX_PAGES))]
    else:
        walked = pages
    by_area = by_pixels = 0
    seen_forms: list = []

    def mul(m, n):
        a, b, c, d, e, f = m; A, B, C, D, E, F = n
        return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)

    def walk(stream_owner, resources, ctm, page_area, depth):
        nonlocal by_area, by_pixels, seen_forms
        try:
            content = ContentStream(stream_owner, reader) if depth else stream_owner.get_contents()
        except Exception:
            return
        if content is None:
            return
        xobjs = (resources or {}).get("/XObject") if resources else None
        xobjs = xobjs.get_object() if hasattr(xobjs, "get_object") else xobjs
        stack = []
        try:
            ops = content.operations
        except Exception:
            return
        for operands, op in ops:
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else ctm
            elif op == b"cm" and len(operands) == 6:
                try:
                    ctm = mul(tuple(float(x) for x in operands), ctm)
                except Exception:
                    pass
            elif op == b"INLINE IMAGE":  # BI ... ID ... EI: drawn into the current transformation
                area = abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])
                if page_area and area >= SIGNIFICANT_PAGE_FRACTION * page_area:
                    by_area += 1
                try:
                    st = operands.get("settings", {}) if isinstance(operands, dict) else {}
                    w = int(st.get("/W", st.get("/Width", 0)) or 0); h = int(st.get("/H", st.get("/Height", 0)) or 0)
                    nbytes = len(operands.get("data", b"") or b"") if isinstance(operands, dict) else 0
                except Exception:
                    w = h = nbytes = 0
                if w * h >= SIGNIFICANT_MIN_PIXELS and nbytes >= SIGNIFICANT_MIN_BYTES:
                    by_pixels += 1
            elif op == b"Do" and xobjs and operands:
                try:
                    x = xobjs[operands[0]].get_object()
                except Exception:
                    continue
                sub = x.get("/Subtype")
                if sub == "/Image":
                    area = abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])
                    if page_area and area >= SIGNIFICANT_PAGE_FRACTION * page_area:
                        by_area += 1
                    try:
                        val = lambda k: x.get(k).get_object() if hasattr(x.get(k), "get_object") else x.get(k, 0)
                        w, h = int(val("/Width") or 0), int(val("/Height") or 0)
                        nbytes = int(val("/Length") or 0) or len(getattr(x, "_data", b"") or b"")
                    except Exception:
                        w = h = nbytes = 0
                    if w * h >= SIGNIFICANT_MIN_PIXELS and nbytes >= SIGNIFICANT_MIN_BYTES:
                        by_pixels += 1
                elif sub == "/Form" and depth < 8 and not any(x is f for f in seen_forms):  # cycle guard: active path only
                    m = x.get("/Matrix")
                    try:
                        fm = tuple(float(v) for v in m) if m else (1, 0, 0, 1, 0, 0)
                    except Exception:
                        fm = (1, 0, 0, 1, 0, 0)
                    seen_forms.append(x)
                    walk(x, x.get("/Resources") or resources, mul(fm, ctm), page_area, depth + 1)  # inherit when omitted
                    seen_forms.pop()

    for page in walked:
        try:
            box = page.mediabox
            page_area = abs(float(box.width) * float(box.height))
            seen_forms = []
            walk(page, page.get("/Resources"), (1, 0, 0, 1, 0, 0), page_area, 0)
        except Exception:
            continue
    scale = len(pages) / len(walked)
    return round(by_area * scale), round(by_pixels * scale)


def _count_embedded_images(data: bytes) -> tuple[int, int]:
    """Count XObject images referenced from page resources.

    Returns (total_refs, unique_objects). Unique tracks distinct PDF
    indirect-object references, a coarse stand-in for the platform's
    de-duplication of visually identical images: a logo embedded once and
    referenced by every page counts as one unique object, while the
    same visual logo re-encoded per page counts as N. So `unique` is the
    expected band and `total_refs` the high band.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, PDFSyntaxError):
        return 0, 0
    total = 0
    seen_object_ids: set[tuple[int, int]] = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        xobjects = resources.get("/XObject")
        if xobjects is None:
            continue
        try:
            for name in xobjects:
                obj = xobjects.raw_get(name) if hasattr(xobjects, "raw_get") else xobjects[name]
                # Resolve through IndirectObject if needed
                target = obj.get_object() if hasattr(obj, "get_object") else obj
                if target.get("/Subtype") != "/Image":
                    continue
                total += 1
                # IndirectObject exposes (idnum, generation); fall back
                # to id() for inline / non-indirect refs.
                if hasattr(obj, "idnum") and hasattr(obj, "generation"):
                    key = (obj.idnum, obj.generation)
                else:
                    key = (id(target), 0)
                seen_object_ids.add(key)
        except Exception:
            continue
    return total, len(seen_object_ids)


def _extract_text_fast(path: Path, data: bytes) -> str:
    """Use pdftotext (Poppler) if available, fall back to pdfminer.

    pdftotext is C-compiled and processes ~100 pages/sec on digital
    PDFs; pdfminer.six processes ~5-10 pages/sec. On a multi-MB
    text-dense PDF that's a 10-20× speedup, which is the difference
    between a usable corpus scan and a multi-day one.
    """
    if _PDFTOTEXT:
        try:
            result = subprocess.run(
                [_PDFTOTEXT, "-q", "-layout", "-enc", "UTF-8", str(path), "-"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        return extract_text(io.BytesIO(data)) or ""
    except Exception:
        return ""


def estimate_pdf(path: Path, fast: bool = False) -> tuple[CreditBand, dict]:
    """Estimate a PDF's unit count.

    fast=False (default): full per-file analysis — page count,
        per-page text extraction, embedded-image enumeration. Most
        accurate, but ~1-3 sec per file.

    fast=True: skip text extraction and embedded-image enumeration.
        Use file-size as a corner alongside page count. ~10-50× faster
        per file. Still uses pdftotext when available, but only for
        cheap header reads, not full extraction.
    """
    data = path.read_bytes()
    # A PDF that cannot be parsed (encrypted, damaged) raises: the scanner then counts it by size only
    # and reports it, instead of inventing pages and tokens.
    raw_pages, pages = _count_pages(data)

    if fast:
        # Fast path: page count (cheap via pypdf trailer) + pdftotext
        # for the token side if Poppler is available. Skip
        # embedded-image enumeration (the slow part). Skip filesize
        # corner for PDFs — PDF filesize is dominated by embedded
        # image bytes, NOT by text-dense content, so it produces
        # wildly wrong estimates on image-heavy decks. Page count is
        # the only consistently-meaningful PDF signal.
        if _PDFTOTEXT:
            try:
                result = subprocess.run(
                    [_PDFTOTEXT, "-q", "-enc", "UTF-8", str(path), "-"],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                text = result.stdout.decode("utf-8", errors="replace") if result.returncode == 0 else ""
            except (subprocess.TimeoutExpired, OSError):
                text = ""
        else:
            text = ""
        raw_tokens = count_tokens(text) if text else pages * _SCANNED_PAGE_TOKEN_FLOOR
        body_tokens = apply_fast_overhead(raw_tokens, "pdf")
        band = band_for(
            pages=pages,
            tokens_low=body_tokens,
            tokens_expected=body_tokens,
            tokens_high=body_tokens,
        )
        return band, {
            "kind": "pdf",
            "pages": pages,
            "pages_raw": raw_pages,
            "presplit_adjusted": pages != raw_pages,
            "fast_mode": True,
            "pdftotext_used": _PDFTOTEXT is not None and bool(text),
            "body_tokens_extracted": raw_tokens,
            "body_tokens_with_overhead": body_tokens,
            "tokens_low": body_tokens,
            "tokens_expected": body_tokens,
            "tokens_high": body_tokens,
        }

    text = _extract_text_fast(path, data)
    body_tokens = count_tokens(text)
    # Pages without a usable text layer go through OCR in the platform. pdftotext and
    # pdfminer both separate pages with form feeds; count the near-empty ones. When the
    # split does not line up with the page count, fall back to the document-wide verdict.
    page_texts = text.split("\x0c")
    try:
        splits = _page_splits(data)  # oversized pages are split before OCR: count billed pages per source page
    except Exception:
        splits = [1] * raw_pages
    if len(page_texts) >= max(1, raw_pages // 2):
        ocr_pages_est = sum(splits[i] for i in range(min(raw_pages, len(splits)))
                            if i >= len(page_texts) or len(page_texts[i].strip()) < 200)
    else:
        ocr_pages_est = pages if body_tokens < raw_pages * 50 else 0
    ocr_pages_est = min(ocr_pages_est, pages)

    n_images_total, n_images_unique = _count_embedded_images(data)
    n_images_significant, n_images_sig_pixels = _significant_images(data)
    # description text is billed for images that get described: those drawn at >= 5 % of the page
    img_low = n_images_significant * SHORT_TOKENS_LOW
    img_exp = n_images_significant * SHORT_TOKENS_EXPECTED
    img_high = n_images_significant * SHORT_TOKENS_HIGH

    is_scanned = body_tokens < (pages * 50)  # virtually no extractable text
    if is_scanned:
        scanned_floor = pages * _SCANNED_PAGE_TOKEN_FLOOR
        body_low = pages * _SCANNED_LOW_TOK
        body_exp = scanned_floor
        body_high = pages * _SCANNED_HIGH_TOK
    else:
        # mixed document: pages without a text layer are OCR'd and their text is billed like any text
        body_low = body_tokens + ocr_pages_est * _SCANNED_LOW_TOK
        body_exp = body_tokens + ocr_pages_est * _SCANNED_PAGE_TOKEN_FLOOR
        body_high = body_tokens + ocr_pages_est * _SCANNED_HIGH_TOK

    band = band_for(
        pages=pages,
        tokens_low=body_low + img_low,
        tokens_expected=body_exp + img_exp,
        tokens_high=body_high + img_high,
    )
    detail = {
        "kind": "pdf",
        "pages": pages,
        "pages_raw": raw_pages,
        "presplit_adjusted": pages != raw_pages,
        "scanned": is_scanned,
        "approx": bool(ocr_pages_est),  # OCR'd text is estimated, not read
        "ocr_pages_est": ocr_pages_est,
        "body_tokens_extracted": body_tokens,
        "n_images_total": n_images_total,
        "n_images_unique": n_images_unique,
        "n_images_significant": n_images_significant,  # drawn at >= 5 % of the page (the ones that get described)
        "n_images_sig_pixels": n_images_sig_pixels,    # >= 100x100 px and >= 5 KB
        "pdftotext_used": _PDFTOTEXT is not None,
        "tokens_low": body_low + img_low,
        "tokens_expected": body_exp + img_exp,
        "tokens_high": body_high + img_high,
    }
    return band, detail
