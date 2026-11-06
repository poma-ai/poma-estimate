"""Magic-byte file-type detection.

Extension-based dispatch is fast but lies. Real-world examples we've
already hit:
  - `tumblr_xyz.gif.html` is a GIF file with an HTML extension
  - copy-pasted `something.pdf` that's actually an HTML error page
  - `data.xlsx` that's actually OOXML PowerPoint (slideshow content-type)

We sniff the first 16 bytes and return the *real* extension when it
disagrees with the user-visible one. Returns None when the file looks
like what its extension claims, so the fast path skips it entirely.
"""

from pathlib import Path


def detect_real_ext(path: Path, claimed_ext: str) -> str | None:
    """Return a corrected extension if the magic bytes disagree.

    Returns None when:
      - the file matches its extension
      - we don't recognize the magic bytes (let the caller try anyway)
      - we can't read the file
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4096)  # enough to see an <svg> root after an XML declaration
    except OSError:
        return None
    if not head:
        return None

    real = _magic_to_ext(head)
    if real is None:
        return None
    if _ext_family(real) == _ext_family(claimed_ext):
        return None  # extension is in the right family, no correction
    return real


def _magic_to_ext(head: bytes) -> str | None:
    """Map header bytes to a canonical extension, or None if unknown."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "tiff"
    if head[:4] in (b"ftyp",) or head[4:12] == b"ftypheic" or head[4:12] == b"ftypheix":
        return "heic"
    if head.startswith(b"PK\x03\x04"):
        # OOXML / ODF / EPUB — can't disambiguate from header alone.
        # Return "zip" as a hint; caller can probe further if needed.
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # OLE compound — .doc / .xls / .ppt
        return "ole"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    # HTML / XML — be lenient about leading whitespace + BOM
    stripped = head.lstrip().lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html"):
        return "html"
    if stripped.startswith(b"<?xml"):
        root = stripped[:4096]
        if b"<svg" in root:
            return "svg"
        if b"<html" in root or b"<!doctype html" in root:
            return "html"
        return "xml"
    return None


_FAMILIES = {
    "pdf":   {"pdf"},
    "image": {"gif", "png", "jpeg", "jpg", "jpe", "webp", "bmp", "tiff", "tif",
              "heic", "heif", "ico", "svg"},
    "ooxml": {"zip", "docx", "dotx", "pptx", "potx", "ppsx", "xlsx", "xltx",
              "odt", "ods", "odp", "epub"},
    "ole":   {"ole", "doc", "xls", "ppt", "xlsb"},
    "rtf":   {"rtf"},
    "html":  {"html", "htm", "xhtml", "shtml", "mhtml", "aspx"},
    "xml":   {"xml", "xsd", "xsl", "xslt", "rss", "atom", "kml", "opml",
              "gpx", "tcx"},
}


def _ext_family(ext: str) -> str:
    for family, members in _FAMILIES.items():
        if ext in members:
            return family
    return ext  # singleton family
