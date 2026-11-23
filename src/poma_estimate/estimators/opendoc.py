"""OpenDocument formats: .odt, .ods, .odp.

ODF is a zip with content.xml + meta.xml. We pull text via odfpy and
count embedded images by scanning the Pictures/ folder in the zip.
"""

import zipfile
from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.estimators.office import (
    SHORT_TOKENS_EXPECTED,
    SHORT_TOKENS_HIGH,
    SHORT_TOKENS_LOW,
)
from poma_estimate.tokenizer import count_tokens

OPENDOC_EXTS = {"odt", "ods", "odp"}

_CHARS_PER_PAGE_FALLBACK = 3000


def _read_text_via_odfpy(path: Path) -> str:
    """Try odfpy's text walker, fall back to raw XML strip if unavailable."""
    try:
        from odf.opendocument import load  # type: ignore[import-untyped]
        from odf.element import Element  # type: ignore[import-untyped]
        from odf import text as odf_text  # type: ignore[import-untyped]
    except ImportError:
        # Fallback: extract content.xml and strip tags.
        with zipfile.ZipFile(path) as zf:
            content = zf.read("content.xml").decode("utf-8", errors="replace")
        import re
        return re.sub(r"<[^>]+>", " ", content)

    doc = load(str(path))
    chunks: list[str] = []

    _REPEAT_CAP = 10_000  # bound the expansion of repeated rows/cells
    _EXPAND_BUDGET = 2_000_000  # characters added by repetition, whole document
    expanded, truncated = [0], [False]

    def _repeat(node) -> int:
        for (_, local), v in getattr(node, "attributes", {}).items():  # odfpy keys are (namespace, local name)
            if local in ("number-rows-repeated", "number-columns-repeated") and v:
                try:
                    return max(1, min(int(v), _REPEAT_CAP))
                except ValueError:
                    return 1
        return 1

    def _walk(node: object) -> None:
        if isinstance(node, Element):
            n = _repeat(node) if getattr(node, "qname", (None, ""))[1] in ("table-row", "table-cell") else 1
            if n > 1:
                start = len(chunks)
                for child in node.childNodes:
                    _walk(child)
                block = chunks[start:]
                if block:
                    size = sum(len(b) for b in block)
                    room = max(0, _EXPAND_BUDGET - expanded[0])  # bound total expansion across the document
                    reps = min(n - 1, room // max(1, size))
                    chunks.extend(block * reps)
                    expanded[0] += reps * size
                    if reps < n - 1:
                        truncated[0] = True
                return
            for child in node.childNodes:
                _walk(child)
        else:
            data = getattr(node, "data", None)
            if data:
                chunks.append(str(data))

    # doc.text exists for .odt only; .ods/.odp keep their content under spreadsheet/presentation
    _walk(doc.body)
    return "\n".join(chunks)


def _count_pictures(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(1 for n in zf.namelist() if n.startswith("Pictures/") and not n.endswith("/"))


def estimate_opendoc(path: Path) -> tuple[CreditBand, dict]:
    ext = path.suffix.lstrip(".").lower()
    text = _read_text_via_odfpy(path)
    body_tokens = count_tokens(text)
    from poma_estimate.estimators.office import _media_images
    n_images = _count_pictures(path)
    n_images_significant = _media_images(path, "Pictures/")[1]

    if ext == "odt":
        # paginated text doc — heuristic chars-per-page
        pages = max(1, (len(text) + _CHARS_PER_PAGE_FALLBACK - 1) // _CHARS_PER_PAGE_FALLBACK)
    elif ext == "odp":
        # presentation — slide count via direct XML scan (one
        # draw:page per slide).
        with zipfile.ZipFile(path) as zf:
            content = zf.read("content.xml").decode("utf-8", errors="replace")
        pages = max(1, content.count("<draw:page"))
    else:
        # spreadsheet — 1 page like xlsx
        pages = 1

    img_low = n_images_significant * SHORT_TOKENS_LOW  # only described images produce text
    img_exp = n_images_significant * SHORT_TOKENS_EXPECTED
    img_high = n_images_significant * SHORT_TOKENS_HIGH
    band = band_for(
        pages=pages,
        tokens_low=body_tokens + img_low,
        tokens_expected=body_tokens + img_exp,
        tokens_high=body_tokens + img_high,
    )
    return band, {
        "kind": ext,
        "pages": pages,
        "body_tokens": body_tokens,
        "n_images_total": n_images,
        "n_images_unique": n_images,
        "n_images_significant": n_images_significant,
        "tokens_low": body_tokens + img_low,
        "tokens_expected": body_tokens + img_exp,
        "tokens_high": body_tokens + img_high,
    }
