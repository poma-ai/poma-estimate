"""HTML estimator.

The platform treats HTML as a single page and converts it to markdown
before reading it. The token side that counts is the markdown output,
not the raw HTML.

We mirror that: parse with BeautifulSoup, render via markdownify, count
tokens. Inline `<img>` tags add an embedded-image description band.

Falls back to plain text strip if markdownify can't handle the input
(rare; markdownify is permissive).
"""

import re
from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.estimators.office import (
    SHORT_TOKENS_EXPECTED,
    SHORT_TOKENS_HIGH,
    SHORT_TOKENS_LOW,
)
from poma_estimate.tokenizer import count_tokens

HTML_EXTS = {"html", "htm", "xhtml", "shtml", "mhtml", "aspx"}


def _read_mhtml(raw: bytes) -> tuple[str, int, set]:
    """(HTML body, significant attached images, locations of all attached images) of a MIME web archive."""
    import email, io as _io
    msg = email.message_from_bytes(raw)
    html, images, locations = "", 0, set()
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/html" and not html:
            payload = part.get_payload(decode=True) or b""
            html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif ctype.startswith("image/"):
            for h in ("Content-Location", "Content-ID"):
                v = (part.get(h) or "").strip().strip("<>")
                if v:
                    locations.add(v.lower())
            data = part.get_payload(decode=True) or b""
            if len(data) >= 5_120:
                try:
                    from PIL import Image
                    w, h = Image.open(_io.BytesIO(data)).size
                    images += min(w, h) >= 100
                except Exception:
                    pass
    return html, images, locations


def _read_html(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    m = re.search(rb"<meta[^>]+charset=[\"']?\s*([A-Za-z0-9_\-]+)", raw[:4096], re.I)
    if m:
        try:
            return raw.decode(m.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _significant_img(img) -> bool:
    """An <img> the description step would keep: >= 5 KB and >= 100 x 100 px. Inline data: images are
    decoded and measured; linked images are judged by their width/height attributes, and counted when
    those are absent (unknown size: err high)."""
    src = (img.get("src") or "").strip()
    if src.lower().startswith("data:"):
        import base64, io as _io
        try:
            payload = base64.b64decode(src.split(",", 1)[1], validate=False)
            if len(payload) < 5_120:
                return False
            from PIL import Image
            w, h = Image.open(_io.BytesIO(payload)).size
            return min(w, h) >= 100
        except Exception:
            return False
    def _dim(v):
        try:
            return int(str(v).strip().rstrip("px"))
        except Exception:
            return None
    w, h = _dim(img.get("width")), _dim(img.get("height"))
    return not ((w is not None and w < 100) or (h is not None and h < 100))


def estimate_html(path: Path) -> tuple[CreditBand, dict]:
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    mhtml_images, attached = 0, set()
    if path.suffix.lower() in (".mhtml", ".mht"):
        html, mhtml_images, attached = _read_mhtml(path.read_bytes())
    else:
        html = _read_html(path)

    # Parse first to count images and strip script/style noise out of
    # the token count. BeautifulSoup with lxml is fast and tolerant.
    import warnings
    try:
        from bs4 import XMLParsedAsHTMLWarning
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)  # XHTML is parsed as HTML on purpose
    except ImportError:
        pass
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    n_images = len(soup.find_all("img"))
    # references to MIME-attached images (cid: or Content-Location) are counted from the archive itself, once
    def _attached(src: str) -> bool:
        s = src.strip().lower()
        return s.startswith("cid:") or s in attached
    n_images_significant = mhtml_images + sum(1 for img in soup.find_all("img")
                                              if not _attached(img.get("src") or "") and _significant_img(img))
    # Inline base64 images ("data:" URIs) are pixels, not text: drop the payload
    # so it is not tokenized (a saved web page can carry hundreds of KB of it).
    for tag in soup.find_all(True):
        for attr, v in list(tag.attrs.items()):
            if isinstance(v, str) and v.lstrip().lower().startswith("data:"):
                tag[attr] = ""
    cleaned_html = str(soup)
    try:
        md = markdownify(cleaned_html, heading_style="ATX")
        md = re.sub(r"\n{3,}", "\n\n", md)
    except Exception:
        # Should be rare. Fall back to plain text extraction.
        md = soup.get_text(" ", strip=True)

    body_tokens = count_tokens(md)
    img_low = n_images_significant * SHORT_TOKENS_LOW  # only images that get described produce text
    img_exp = n_images_significant * SHORT_TOKENS_EXPECTED
    img_high = n_images_significant * SHORT_TOKENS_HIGH

    band = band_for(
        pages=1,
        tokens_low=body_tokens + img_low,
        tokens_expected=body_tokens + img_exp,
        tokens_high=body_tokens + img_high,
    )
    return band, {
        "kind": "html",
        "pages": 1,
        "body_tokens": body_tokens,
        "n_images_total": n_images,
        "n_images_unique": n_images,
        "n_images_significant": n_images_significant,
        "tokens_low": body_tokens + img_low,
        "tokens_expected": body_tokens + img_exp,
        "tokens_high": body_tokens + img_high,
    }
