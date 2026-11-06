"""Text/markup/structured-data estimator.

All plain-text formats count as 1 page. Token side is the raw text
passed through the tokenizer.

Markup-stripping is intentionally cheap (regex-based) — close enough for
estimation, and it never crashes on malformed input the way an HTML/XML
parser would.
"""

import re
from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.tokenizer import count_tokens

# NOTE: html / htm / xhtml / shtml / mhtml / aspx live in html_doc.py
# (proper markdownify-based estimator). This module covers plain text,
# markdown, structured data, and config formats — everything in
# the plain-text family minus HTML.
TEXT_EXTS = {
    # plain text and markdown
    "txt", "log", "text",
    "md", "markdown", "mdown", "mkd", "mkdn",
    # structured data
    "xml", "xsd", "xsl", "xslt", "rss", "atom", "kml", "opml", "gpx", "tcx",
    "json", "json5", "geojson",
    "yaml", "yml",
    "toml",
    "ini", "cfg", "conf",
    "env",
    # SPICE netlist
    "cir", "sp", "spice", "ckt",
    # tabular
    "csv",
    "tsv", "tab",
}

_TAG_RE = re.compile(r"<[^>]+>")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)", re.I)
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_DIM_RE = re.compile(r"\b(width|height)\s*=\s*[\"']?(\d+)", re.I)


_DATA_URI_RUN_RE = re.compile(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=\s]*", re.I)
_LEADING_B64_RE = re.compile(r"\A[A-Za-z0-9+/=]{512,}")  # only at a window start, where a payload may continue
_MD_EXTS = frozenset({"md", "markdown", "mdown", "mkd", "mkdn", "mdwn", "mdx"})
_DATA_IMG_RE = re.compile(r"!\[[^\]]*\]\((data:image/[^;,)]+;base64,[^)\s]+)\)|<img\b[^>]*?src=[\"'](data:image/[^;,\"']+;base64,[^\"']+)[\"'][^>]*>", re.I)


def _strip_md_data_images(raw: str) -> tuple[str, int]:
    """Remove inline base64 images (pixels, not text) and count those the description step would keep
    (>= 5 KB and >= 100 x 100 px)."""
    import base64, io as _io
    kept = 0

    def _sub(m):
        nonlocal kept
        uri = m.group(1) or m.group(2)
        try:
            payload = base64.b64decode(uri.split(",", 1)[1], validate=False)
            if len(payload) >= 5_120:
                from PIL import Image
                w, h = Image.open(_io.BytesIO(payload)).size
                kept += min(w, h) >= 100
        except Exception:
            pass
        return " "

    return _DATA_IMG_RE.sub(_sub, raw), kept


def _md_images(raw: str) -> int:
    """Markdown image references plus <img> tags, minus tags declared smaller than 100 px on a side
    (tracking pixels, icons): the size rule the description step applies. Unknown sizes count."""
    n = len(_MD_IMG_RE.findall(raw))
    for tag in _HTML_IMG_RE.findall(raw):
        dims = [int(v) for _, v in _DIM_RE.findall(tag)]
        if not dims or min(dims) >= 100:
            n += 1
    return n
_WS_RE = re.compile(r"\s+")


def _strip_markup(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _detect_encoding(head: bytes) -> tuple[str, int]:
    """(codec, code-unit width) from a BOM, or from NUL bytes in BOM-less UTF-16 (Windows exports)."""
    if head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32", 4
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        return "utf-16", 2
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", 1
    sample = head[:4096]
    if len(sample) >= 64 and sample.count(0) > len(sample) // 4:
        odd = sum(1 for b in sample[1::2] if b == 0); even = sum(1 for b in sample[0::2] if b == 0)
        return ("utf-16-le" if odd > even else "utf-16-be"), 2
    return "utf-8", 1


def _read_text(path: Path) -> str:
    # Decode by BOM / NUL pattern, never raise: the customer needs an estimate, not a parse error.
    with open(path, "rb") as fh:
        enc, _ = _detect_encoding(fh.read(4096))
    return path.read_bytes().decode(enc, errors="replace")


_SAMPLE_OVER = 16_000_000  # bytes; larger files are sampled, not read whole
_SAMPLE_CHUNK = 256_000
_SAMPLE_WINDOWS = 12


def _sampled_estimate(path: Path, size: int, ext: str) -> tuple[CreditBand, dict]:
    """Tokenize evenly spaced windows and extrapolate by byte ratio. The
    result is flagged `sampled` so callers never call it exact."""
    toks = nbytes = 0
    step = (size - _SAMPLE_CHUNK) // (_SAMPLE_WINDOWS - 1)
    with open(path, "rb") as f:
        enc, width = _detect_encoding(f.read(4096))
        if enc in ("utf-16", "utf-32"):  # BOM only at the start: decode windows with the explicit byte order
            f.seek(0)
            bom = f.read(2)
            enc = ("utf-16-le" if bom == b"\xff\xfe" else "utf-16-be") if width == 2 else ("utf-32-le" if bom == b"\xff\xfe" else "utf-32-be")
        elif enc == "utf-8-sig":
            enc = "utf-8"
        for i in range(_SAMPLE_WINDOWS):
            f.seek((i * step) // width * width)  # align windows to whole code units
            raw = f.read(_SAMPLE_CHUNK).decode(enc, errors="replace")
            if ext in _MD_EXTS:  # inline image payloads are pixels, not text
                raw = _DATA_URI_RUN_RE.sub(" ", raw)
                if i:  # a window can start inside a payload that began in an earlier window
                    m = _LEADING_B64_RE.match(raw)
                    run = m.group(0) if m else ""
                    # encoded binary has mixed case and + or /; long digit or word runs are text
                    if run and any(c.isupper() for c in run[:4096]) and any(c.islower() for c in run[:4096]) \
                            and ("+" in run or "/" in run):
                        raw = raw[len(run):]
            if ext in _XML_EXTS:
                raw = _strip_markup(raw)
            toks += count_tokens(raw)
            nbytes += _SAMPLE_CHUNK
    body = max(1, int(toks * size / nbytes))
    n_img = 0
    if ext in _MD_EXTS:  # count image references over the whole file (a byte scan); sizes unknown: err high
        with open(path, "rb") as f:
            tail = b""
            while chunk := f.read(4 << 20):
                buf = tail + chunk
                n_img += buf.count(b"![") + buf.lower().count(b"<img")  # inline data: images sit inside these
                tail = buf[-16:]
                n_img -= tail.count(b"![") + tail.lower().count(b"<img")
            n_img += tail.count(b"![") + tail.lower().count(b"<img")
    from poma_estimate.estimators.office import SHORT_TOKENS_EXPECTED
    exp = body + n_img * SHORT_TOKENS_EXPECTED
    low, high = int(exp * 0.6), int(exp * 1.5)
    return band_for(1, low, exp, high), {
        "kind": "text", "ext": ext, "sampled": True, "pages": 1, "body_tokens": body, "n_images_significant": n_img,
        "tokens_low": low, "tokens_expected": exp, "tokens_high": high,
    }


_XML_EXTS = ("xml", "xsd", "xsl", "xslt", "rss", "atom", "kml", "opml", "gpx", "tcx")


def estimate_text(path: Path) -> tuple[CreditBand, dict]:
    size = path.stat().st_size
    if size > _SAMPLE_OVER:
        return _sampled_estimate(path, size, path.suffix.lstrip(".").lower())
    raw = _read_text(path)
    ext = path.suffix.lstrip(".").lower()
    # XML-family files get markup-stripped — text estimator is correct
    # for them. HTML is routed to html_doc.py, not here.
    md_inline = 0
    if ext in _MD_EXTS:
        raw, md_inline = _strip_md_data_images(raw)
    if ext in _XML_EXTS:
        body = _strip_markup(raw)
    else:
        body = raw
    tokens = count_tokens(body)
    n_sig = (_md_images(raw) + md_inline) if ext in _MD_EXTS else 0
    from poma_estimate.estimators.office import SHORT_TOKENS_EXPECTED, SHORT_TOKENS_HIGH, SHORT_TOKENS_LOW
    band = band_for(pages=1, tokens_low=tokens + n_sig * SHORT_TOKENS_LOW, tokens_expected=tokens + n_sig * SHORT_TOKENS_EXPECTED,
                    tokens_high=tokens + n_sig * SHORT_TOKENS_HIGH)
    detail = {
        "kind": "text",
        "ext": ext,
        "char_count": len(raw),
        # Markdown image references (![alt](src) or <img>): each is fetched and described like an embedded image
        "n_images_significant": n_sig,
        "body_tokens": tokens,
        "tokens_low": tokens + n_sig * SHORT_TOKENS_LOW,
        "tokens_expected": tokens + n_sig * SHORT_TOKENS_EXPECTED,
        "tokens_high": tokens + n_sig * SHORT_TOKENS_HIGH,
        "pages": 1,
    }
    return band, detail
