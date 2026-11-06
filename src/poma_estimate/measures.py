"""What the tool reports besides units: plain counts per file-type group.

Every number here is a count a customer can check against their own files (files, bytes, pages,
pages without a text layer, extracted text tokens, images that get described, slides that get
read visually, files that need a format conversion). No prices and no weights live in this module.
"""

from __future__ import annotations

# Report order and labels.
GROUPS = [
    ("pdf", "PDF"),
    ("converted_docs", "Other documents converted to PDF (EPUB, XPS, multi-page TIFF, ...)"),
    ("word", "Word and text documents (DOCX, DOC, ODT, RTF, Pages)"),
    ("excel", "Spreadsheets (XLSX, XLS, ODS, Numbers)"),
    ("powerpoint", "Presentations (PPTX, PPT, ODP, Key)"),
    ("markdown", "Markdown"),
    ("html", "Web pages (HTML, MHTML)"),
    ("text", "Plain text and data (TXT, CSV, JSON, ...)"),
    ("xml", "XML"),
    ("image", "Images"),
    ("audio", "Audio"),
    ("video", "Video"),
    ("archive", "Archives (ZIP, TAR, RAR, 7Z)"),
    ("other", "Other supported files"),
]
LABELS = dict(GROUPS)

# Counted fields per group, in report order. "files" and "bytes" exist for every group.
FIELDS = {
    "pdf": ["pages", "scanned_pages", "scanned_files", "tokens", "images"],
    "converted_docs": ["pages", "scanned_pages", "tokens", "images", "converted_files"],
    "word": ["pages", "tokens", "images", "converted_files"],
    "excel": ["tokens", "images", "converted_files"],
    "powerpoint": ["slides", "visual_slides", "files_with_visual_slides", "tokens", "images", "converted_files"],
    "markdown": ["tokens", "images"],
    "html": ["tokens", "images"],
    "text": ["tokens"],
    "xml": ["tokens"],
    "image": ["frames", "large_files", "converted_files"],
    "audio": ["minutes"],
    "video": ["minutes"],
    "archive": ["contained_files", "contained_bytes", "unlisted_files"],
    "other": [],
}

CANON = {"ppsx": "pptx", "potx": "pptx", "pptm": "pptx", "dotx": "docx", "docm": "docx", "dotm": "docx",
         "xltx": "xlsx", "xlsm": "xlsx", "jpeg": "jpg", "jpe": "jpg", "tif": "tiff",
         **{e: "md" for e in ("markdown", "mdown", "mkd", "mkdn", "mdwn", "mdx")},
         **{e: "html" for e in ("htm", "xhtml", "shtml", "aspx")}}


def canonical(ext: str) -> str:
    return CANON.get(ext, ext)


WORD_EXTS = {"docx", "dotx", "docm", "dotm", "doc", "odt", "rtf", "pages"}
EXCEL_EXTS = {"xlsx", "xltx", "xlsm", "xls", "xlsb", "ods", "numbers"}
PPT_EXTS = {"pptx", "potx", "ppsx", "pptm", "ppt", "pps", "pot", "odp", "key"}
MD_EXTS = {"md", "markdown", "mdown", "mkd", "mkdn", "mdwn", "mdx"}
HTML_EXTS = {"html", "htm", "xhtml", "shtml", "mhtml", "mht", "aspx", "webarchive"}
XML_EXTS = {"xml", "xsd", "xsl", "xslt", "rss", "atom", "kml", "opml", "gpx", "tcx"}
TO_PDF_EXTS = {"tiff", "tif", "eps", "epub", "mobi", "djvu", "xps", "oxps", "ps", "prn", "pub", "mdi",
               "vsd", "vsdx", "odc", "odf"}
# Formats the platform converts before reading them (reported as files needing format conversion).
FORMAT_CONVERSION_EXTS = {"pages", "ppt", "pps", "pot", "odp", "key", "xls", "ods", "numbers",
                            "bmp", "heic", "heif", "ico", "webp"} | TO_PDF_EXTS
LARGE_IMAGE_BYTES = 1_000_000
GIF_MAX_KEYFRAMES = 6


def group_of(ext: str, kind: str | None) -> str:
    if kind == "archive":
        return "archive"
    if kind in ("audio", "video"):
        return kind
    if ext == "pdf":
        return "pdf"
    if ext in ("tiff", "tif"):
        return "converted_docs" if kind == "image" else "image"
    if kind == "image":
        return "image"
    if ext in TO_PDF_EXTS:
        return "converted_docs"
    if ext in WORD_EXTS:
        return "word"
    if ext in EXCEL_EXTS:
        return "excel"
    if ext in PPT_EXTS:
        return "powerpoint"
    if ext in MD_EXTS:
        return "markdown"
    if ext in HTML_EXTS:
        return "html"
    if ext in XML_EXTS:
        return "xml"
    if kind == "text":
        return "text"
    return "other"


def _tokens(d: dict) -> float:
    for k in ("body_tokens_extracted", "body_tokens"):
        if d.get(k) is not None:
            return float(d[k])
    return float(d.get("tokens_expected") or 0)


def _images(d: dict) -> float:
    v = d.get("n_images_significant")
    if v is None:
        v = d.get("n_images_unique", d.get("n_images", 0))
    return float(v or 0)


def measures_for(detail: dict, ext: str, size_bytes: int) -> tuple[str, dict]:
    """(group, counts) for one measured file."""
    d = detail or {}
    kind = d.get("kind")
    g = group_of(ext, kind)
    m = {"files": 1, "bytes": size_bytes}
    pages = float(d.get("pages") or 1)
    conv = 1 if ext in FORMAT_CONVERSION_EXTS else 0
    if g == "pdf":
        est = d.get("ocr_pages_est")
        scan = float(est) if est is not None else (pages if d.get("scanned") else 0.0)
        m.update(pages=pages, scanned_pages=scan, scanned_files=1 if d.get("scanned") else 0,
                 tokens=_tokens(d), images=_images(d))
    elif g == "converted_docs":
        if kind == "image":  # multi-page TIFF: each frame becomes a scanned page with its page image
            frames = float(max(1, int(d.get("n_frames") or 1)))
            m.update(pages=frames, scanned_pages=frames, tokens=0.0, images=frames, converted_files=conv)
        else:
            m.update(pages=pages, scanned_pages=0.0, tokens=_tokens(d), images=_images(d), converted_files=conv)
    elif g == "word":
        m.update(pages=pages, tokens=_tokens(d), images=_images(d), converted_files=conv)
    elif g == "excel":
        m.update(tokens=_tokens(d), images=_images(d), converted_files=conv)
    elif g == "powerpoint":
        visual = d.get("visual_slides")
        visual = float(visual if visual is not None else pages)
        m.update(slides=pages, visual_slides=visual, files_with_visual_slides=1 if visual else 0,
                 tokens=_tokens(d), images=_images(d), converted_files=conv)
    elif g in ("markdown", "html"):
        m.update(tokens=_tokens(d), images=_images(d))
    elif g in ("text", "xml"):
        m.update(tokens=_tokens(d))
    elif g == "image":
        frames = max(1, int(d.get("n_frames") or 1)) if ext == "gif" else 1
        m.update(frames=float(min(frames, GIF_MAX_KEYFRAMES)),
                 large_files=1 if (d.get("size_bytes") or size_bytes) >= LARGE_IMAGE_BYTES else 0,
                 converted_files=conv)
    elif g in ("audio", "video"):
        m.update(minutes=float(d.get("minutes") or 0.0))
    elif g == "archive":
        listed = str(d.get("archive_toc", "")) == "listed"
        m.update(contained_files=float(d.get("archive_supported_members") or 0) if listed else 0.0,
                 contained_bytes=float(d.get("archive_supported_bytes") or 0) if listed else 0.0,
                 unlisted_files=0 if listed else 1)
    return g, m


def size_only(ext: str, kind: str | None, size_bytes: int) -> tuple[str, dict]:
    """(group, counts) for a file that could not be read: counted by number and size only."""
    return group_of(ext, kind), {"files": 1, "bytes": size_bytes, "unmeasured_files": 1, "unmeasured_bytes": size_bytes}


def counts_for_result(r) -> tuple[str, dict]:
    """(group, counts) for a FileResult: measured counts, or size-only for an unreadable file."""
    if r.error:
        from poma_estimate.scan import _classify
        return size_only(r.ext, _classify(r.ext), r.size_bytes)
    return measures_for(r.detail, r.ext, r.size_bytes)


def empty_totals() -> dict:
    return {}


def add(totals: dict, group: str, m: dict, scale: float = 1.0) -> None:
    t = totals.setdefault(group, {"files": 0.0, "bytes": 0.0, "unmeasured_files": 0.0, "unmeasured_bytes": 0.0})
    for k, v in m.items():
        t[k] = t.get(k, 0.0) + v * scale


def add_unmeasured(totals: dict, group: str, n_files: int, n_bytes: int) -> None:
    """Files in strata with no measured sample: counted by number and size only."""
    t = totals.setdefault(group, {"files": 0.0, "bytes": 0.0, "unmeasured_files": 0.0, "unmeasured_bytes": 0.0})
    t["files"] += n_files
    t["bytes"] += n_bytes
    t["unmeasured_files"] += n_files
    t["unmeasured_bytes"] += n_bytes


def rounded(totals: dict) -> dict:
    """Integer counts in report order (extrapolated values rounded)."""
    out = {}
    for g, _ in GROUPS:
        if g in totals and totals[g].get("files"):
            t = totals[g]
            out[g] = {k: int(round(t.get(k, 0.0))) for k in ["files", "bytes"] + FIELDS[g] + ["unmeasured_files", "unmeasured_bytes"]}
    return out


def _fmt_bytes(n: int) -> str:
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:,.1f} {unit}"
    return f"{n} B"


_NAMES = {
    "pages": "pages", "scanned_pages": "pages without a text layer", "scanned_files": "fully scanned files",
    "tokens": "text tokens", "images": "images", "converted_files": "files needing format conversion",
    "slides": "slides", "visual_slides": "slides with visual content", "files_with_visual_slides": "decks with visual slides",
    "frames": "image frames", "large_files": "images of 1 MB or more", "minutes": "minutes",
    "contained_files": "files inside", "contained_bytes": "bytes inside", "unlisted_files": "archives that could not be listed",
}


_SINGULAR = {"pages": "page", "scanned_pages": "page without a text layer", "scanned_files": "fully scanned file",
             "tokens": "text token", "images": "image", "converted_files": "file needing format conversion",
             "slides": "slide", "visual_slides": "slide with visual content", "files_with_visual_slides": "deck with visual slides",
             "frames": "image frame", "large_files": "image of 1 MB or more", "minutes": "minute",
             "contained_files": "file inside", "unlisted_files": "archive that could not be listed"}


def _n(v: int, one: str, many: str) -> str:
    return f"{v:,} {one if v == 1 else many}"


def describe(counts: dict) -> list[str]:
    """One human-readable line per group, names and counts only."""
    lines = []
    for g, label in GROUPS:
        c = counts.get(g)
        if not c:
            continue
        parts = [_n(c["files"], "file", "files"), _fmt_bytes(c["bytes"])]
        for k in FIELDS[g]:
            v = c.get(k, 0)
            if k == "contained_bytes":
                if v:
                    parts.append(f"{_fmt_bytes(v)} inside")
                continue
            if v:
                parts.append(f"{v:,} {_SINGULAR.get(k, _NAMES[k]) if v == 1 else _NAMES[k]}")
        if c.get("unmeasured_files"):
            parts.append(_n(c["unmeasured_files"], "file", "files") + f" ({_fmt_bytes(c['unmeasured_bytes'])}) counted by size only")
        lines.append(f"{label}: " + ", ".join(parts))
    return lines
