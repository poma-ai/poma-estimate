"""EPUB estimator.

EPUB is a zip with HTML chapters. The platform converts it to PDF before parsing, so the
page side is the rendered page count, estimated from the text of the documents in
the reading spine (documents outside the spine are not rendered). The estimate is
approximate: real pagination depends on fonts, images and page breaks.
"""

from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.estimators.office import (
    SHORT_TOKENS_EXPECTED,
    SHORT_TOKENS_HIGH,
    SHORT_TOKENS_LOW,
)
from poma_estimate.tokenizer import count_tokens

EPUB_EXTS = {"epub"}

_CHARS_PER_PAGE = 3000  # same heuristic as .odt
import re as _re
# one match per declaration: page-break-before/after:always, break-before/after:page, epub:type="pagebreak"
_BREAK_RE = _re.compile(rb"(?:page-break-(?:before|after)\s*:\s*always|(?<![-\w])break-(?:before|after)\s*:\s*page|pagebreak\")")


def estimate_epub(path: Path) -> tuple[CreditBand, dict]:
    import ebooklib  # type: ignore[import-untyped]
    from ebooklib import epub  # type: ignore[import-untyped]
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(path))
    text_parts: list[str] = []
    chapters = 0
    n_images = 0
    spine_ids = [entry[0] if isinstance(entry, (tuple, list)) else entry for entry in (book.spine or [])]
    docs = [book.get_item_with_id(i) for i in spine_ids]
    docs = [d for d in docs if d is not None and d.get_type() == ebooklib.ITEM_DOCUMENT]
    if not docs:  # no usable spine: fall back to every document
        docs = [i for i in book.get_items() if i.get_type() == ebooklib.ITEM_DOCUMENT]
    breaks = 0
    pages_sum = 0  # each spine document is paginated on its own: it starts a new page when rendered
    for item in docs:
        chapters += 1
        raw = item.get_content()
        low = raw.lower()
        doc_breaks = len(_BREAK_RE.findall(low))
        breaks += doc_breaks
        soup = BeautifulSoup(raw, "html.parser")
        doc_text = soup.get_text(" ", strip=True)
        text_parts.append(doc_text)
        pages_sum += max(1 + doc_breaks, -(-len(doc_text) // _CHARS_PER_PAGE))
        n_images += len(soup.find_all("img"))

    text = "\n".join(text_parts)
    body_tokens = count_tokens(text)
    # each spine document starts a new page when rendered; explicit page breaks add pages
    pages = max(1, pages_sum)
    img_low = n_images * SHORT_TOKENS_LOW
    img_exp = n_images * SHORT_TOKENS_EXPECTED
    img_high = n_images * SHORT_TOKENS_HIGH
    band = band_for(
        pages=pages,
        tokens_low=body_tokens + img_low,
        tokens_expected=body_tokens + img_exp,
        tokens_high=body_tokens + img_high,
    )
    return band, {
        "kind": "epub",
        "pages": pages,
        "chapters": chapters,
        "approx": True,
        "body_tokens": body_tokens,
        "n_images_total": n_images,
        "n_images_unique": n_images,
        "tokens_low": body_tokens + img_low,
        "tokens_expected": body_tokens + img_exp,
        "tokens_high": body_tokens + img_high,
    }
