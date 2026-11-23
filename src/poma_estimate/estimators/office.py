"""Office-format estimator: DOCX, PPTX, XLSX.

All three are converted to PDF by the platform, then read as PDFs. For estimation
we skip the round-trip and pull text directly from the OOXML containers
— accurate enough on token side, and we can count embedded images for
the description-band addition.

Page count:
  - DOCX: count rendered pages via `lastRenderedPageBreak` markers when
    present, else estimate from char count (2,500 chars/page heuristic).
  - PPTX: 1 slide = 1 page.
  - XLSX: the platform treats all spreadsheets as 1 page; token side
    dominates (large sheets routinely exceed thousands of tokens).
"""

from pathlib import Path

import re
import zipfile

from docx.enum.section import WD_SECTION

from poma_estimate.credit_math import (
    CreditBand,
    apply_fast_overhead,
    band_for,
    corner_max,
    filesize_corner,
)
from poma_estimate.tokenizer import count_tokens

_OOXML_TEXT_RE = re.compile(r"<(?:w|a):t[^>]*>([^<]*)</(?:w|a):t>")
_OOXML_PIC_RE = re.compile(r"<(?:p:pic|w:drawing)\b")
_OOXML_PAGE_BREAK_RE = re.compile(r"w:lastRenderedPageBreak")
_OOXML_EXPLICIT_BREAK_RE = re.compile(r'<w:br\s[^>]*w:type="page"')
# XLSX worksheet cells: <v> holds the value (numeric raw, or string-index
# when the parent <c> has t="s"). For credit estimation we treat every
# raw value as a token-source — numeric cells in a financial workbook
# count for credits just as much as text cells. Without this regex, a
# 200k-row numeric xlsx looks like an empty file.
_XLSX_VALUE_RE = re.compile(r"<v[^>]*>([^<]*)</v>")

# OOXML family — all routed to the matching docx/pptx/xlsx reader.
# Template variants (.dotx/.potx/.xltx) and presentation-show variants
# (.ppsx) use the same OOXML container so the readers handle them.
OFFICE_EXTS = {"docx", "dotx", "docm", "dotm", "pptx", "potx", "ppsx", "pptm", "xlsx", "xltx", "xlsm"}

# Embedded images get a shorter description than image-as-document inputs.
#
# Calibrated 2026-05-19 against 72 images in 21 production .poma archives:
#   p50  =  48
#   p75  = 161
#   p90  = 245
#   p95  = 264
#   p99  = 296
#   max  = 342
# Aggregate share of description tokens vs body text: 0.9%. Pages
# dominate credits in 20/21 archives; tokens dominate only when there's
# essentially no images and lots of text-only content.
SHORT_TOKENS_LOW = 20
SHORT_TOKENS_EXPECTED = 80
SHORT_TOKENS_HIGH = 350

# 2,500 chars/page: measured against LibreOffice's PDF render of 4 real
# DOCX (3,000 under-counted by 15-40%; the token side is unaffected).
_CHARS_PER_PAGE_FALLBACK = 2500
_APP_PAGES_RE = re.compile(r"<Pages>\s*(\d+)\s*</Pages>")


def _choose_pages(app_pages: int | None, heuristic: int, explicit: int, breaks: int) -> tuple[int, str]:
    """Saved page count when plausible, else breaks / text length / explicit breaks."""
    if app_pages and 0.5 * max(heuristic, explicit) <= app_pages <= 3 * max(heuristic, explicit) + 3:
        if app_pages < explicit:  # explicit page breaks are a hard floor
            return explicit, "explicit_breaks"
        return app_pages, "saved_page_count"
    stored = (breaks + 1) if breaks else None
    return max(stored or 1, heuristic, explicit), ("rendered_breaks" if breaks else "char_heuristic")


def _app_xml_pages(path: Path) -> int | None:
    """Page count the authoring app stored in docProps/app.xml (Word and
    LibreOffice both write it); None when absent or zero."""
    try:
        with zipfile.ZipFile(path) as zf:
            m = _APP_PAGES_RE.search(zf.read("docProps/app.xml").decode("utf-8", "replace"))
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


# --- images and visual slides: the counts the tool reports --------------------------------------------
# Significant embedded image: >= 5 KB and >= 100 x 100 px ; smaller ones are
# decorative glyphs/bullets that are not described. Vector parts (EMF/WMF) Pillow cannot size count
# when >= 5 KB.
SIG_MIN_BYTES = 5_120
SIG_MIN_PIXELS = 10_000
SIG_MIN_SIDE = 100


def _media_images(path: Path, prefix: str) -> tuple[int, int]:
    """(media images, significant media images) under e.g. 'word/media/'. Reads at most 256 KB of
    each member (image headers), never the whole archive."""
    import io as _io
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        Image = None
    total = sig = 0
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                total += 1
                if info.file_size < SIG_MIN_BYTES:
                    continue
                if Image is None:
                    sig += 1
                    continue
                try:
                    with zf.open(info) as fh:
                        head = fh.read(256 * 1024)
                    w, h = Image.open(_io.BytesIO(head)).size
                    if min(w, h) >= SIG_MIN_SIDE:
                        sig += 1
                except Exception:
                    sig += 1  # vector or unreadable header: count it, err high
    except (zipfile.BadZipFile, OSError):
        pass
    return total, sig


_PPTX_DRAWINGML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


def _profile_shape(shape, prof: dict) -> None:
    """Profile one shape for the visual-slide rule."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    el = getattr(shape, "_element", None)
    if el is not None:
        gd = el.find(f".//{{{_PPTX_DRAWINGML_NS}}}graphic/{{{_PPTX_DRAWINGML_NS}}}graphicData")
        if gd is not None and gd.get("uri") == _DGM_NS:
            prof["smartart"] += 1
            return
    st = getattr(shape, "shape_type", None)
    if st == MSO_SHAPE_TYPE.GROUP:
        for sub in shape.shapes:
            _profile_shape(sub, prof)
        return
    if st == MSO_SHAPE_TYPE.PICTURE:
        prof["pictures"] += 1
        return
    if getattr(shape, "has_table", False):
        prof["tables"] += 1
        return
    if getattr(shape, "has_chart", False):
        prof["charts"] += 1
        return
    if st == MSO_SHAPE_TYPE.PLACEHOLDER:
        if shape.has_text_frame:
            t = sum(len(p.text) for p in shape.text_frame.paragraphs)
            if t > 0:
                prof["text_chars"] += t; prof["text_frames"] += 1
        return
    if getattr(shape, "has_text_frame", False):
        t = sum(len(p.text) for p in shape.text_frame.paragraphs)
        if t > 0:
            prof["text_chars"] += t; prof["text_frames"] += 1
        else:
            prof["non_text_shapes"] += 1
    else:
        prof["non_text_shapes"] += 1


def _slide_is_visual(slide) -> bool:
    """True when the slide is read visually: a picture, chart, SmartArt or table, or hardly any text."""
    prof = dict(text_chars=0, text_frames=0, pictures=0, tables=0, non_text_shapes=0, charts=0, smartart=0)
    for shape in slide.shapes:
        _profile_shape(shape, prof)
    unrenderable = 0
    if prof["smartart"]:
        try:
            drawn = sum(1 for rel in slide.part.rels.values() if str(getattr(rel, "reltype", "")).endswith("/diagramDrawing"))
        except Exception:
            drawn = 0
        unrenderable = max(0, prof["smartart"] - drawn)
    renderable = prof["smartart"] - unrenderable
    visual = prof["pictures"] + prof["non_text_shapes"] + prof["charts"] + renderable
    if prof["text_chars"] <= 30 and visual == 0 and unrenderable == 0:
        return True
    if prof["charts"] >= 1 or renderable >= 1:
        return True
    if unrenderable and visual == 0:
        return False
    if visual >= 1:
        return True
    return prof["text_frames"] >= 3


def _nearest_p(el, w_p):
    anc = el.getparent()
    while anc is not None and anc.tag != w_p:
        anc = anc.getparent()
    return anc


def _estimate_docx(path: Path) -> tuple[CreditBand, dict]:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(path))
    text_parts: list[str] = []
    # Every paragraph in the body XML, once, in document order: covers tables (each cell element appears
    # once, so merged cells are not repeated), nested tables, content controls (w:sdt) and text boxes.
    # Text boxes are stored twice (mc:Choice and mc:Fallback); the fallback copy is skipped.
    fallback = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
    w_t = qn("w:t")
    w_p = qn("w:p")
    for p_el in doc.element.body.iter(w_p):
        anc, skip = p_el.getparent(), False
        while anc is not None:
            if anc.tag == fallback:
                skip = True
                break
            anc = anc.getparent()
        if skip:
            continue
        # only text whose nearest paragraph is this one: a text box nests paragraphs inside a paragraph
        t = "".join(x.text or "" for x in p_el.iter(w_t) if _nearest_p(x, w_p) is p_el)
        if t:
            text_parts.append(t)
    # footnotes and endnotes live in their own parts; the platform's parser includes them
    try:
        with zipfile.ZipFile(path) as zf:
            for part in ("word/footnotes.xml", "word/endnotes.xml"):
                if part in zf.namelist():
                    text_parts.extend(_OOXML_TEXT_RE.findall(zf.read(part).decode("utf-8", errors="replace")))
    except (zipfile.BadZipFile, OSError):
        pass
    text = "\n".join(text_parts)

    # lastRenderedPageBreak gives us actual page count when Word saved
    # one; otherwise fall back to chars/page heuristic.
    body = doc.element.body
    breaks = body.findall(f".//{qn('w:lastRenderedPageBreak')}")
    heuristic = max(1, (len(text) + _CHARS_PER_PAGE_FALLBACK - 1) // _CHARS_PER_PAGE_FALLBACK)
    def _on(el) -> bool:
        v = el.get(qn("w:val"))
        return v is None or v.lower() not in ("0", "false", "off")

    explicit = (
        len([b for b in body.findall(f".//{qn('w:br')}") if b.get(qn("w:type")) == "page"])
        + len([e for e in body.findall(f".//{qn('w:pageBreakBefore')}") if _on(e)])
        + sum(1 for sec in list(doc.sections)[1:] if sec.start_type in (WD_SECTION.NEW_PAGE, WD_SECTION.ODD_PAGE, WD_SECTION.EVEN_PAGE))
        + 1
    )
    pages, pages_source = _choose_pages(_app_xml_pages(path), heuristic, explicit, len(breaks))

    # Count embedded images via the related parts.
    n_images = 0
    for rel in doc.part.rels.values():
        if "image" in rel.reltype:
            n_images += 1

    body_tokens = count_tokens(text)
    n_sig = _media_images(path, "word/media/")[1]
    img_low = n_sig * SHORT_TOKENS_LOW  # only described images produce billed text
    img_exp = n_sig * SHORT_TOKENS_EXPECTED
    img_high = n_sig * SHORT_TOKENS_HIGH

    band = band_for(
        pages=pages,
        tokens_low=body_tokens + img_low,
        tokens_expected=body_tokens + img_exp,
        tokens_high=body_tokens + img_high,
    )
    detail = {
        "kind": "docx",
        "pages": pages,
        "pages_source": pages_source,
        "approx": pages_source != "saved_page_count" and not (breaks and pages == len(breaks) + 1),
        "body_tokens": body_tokens,
        "n_images": n_images,
        "n_images_unique": n_images,
        "n_images_significant": n_sig,
        "tokens_low": body_tokens + img_low,
        "tokens_expected": body_tokens + img_exp,
        "tokens_high": body_tokens + img_high,
    }
    return band, detail


def _estimate_pptx(path: Path) -> tuple[CreditBand, dict]:
    """Extract slide text + image count from a pptx-family file.

    python-pptx is strict about the OOXML content type, so it refuses
    .ppsx (slideshow) and .potx (template) files even though the
    payload is the same. We try python-pptx first, and on rejection
    fall back to a direct zip walk that pulls `<a:t>` text and
    `<p:pic>` shape counts from each `ppt/slides/slide*.xml`.
    """
    text_parts: list[str] = []
    n_images = 0
    pages = 0
    visual_slides: int | None = None

    try:
        from pptx import Presentation

        prs = Presentation(str(path))
        pages = len(prs.slides)
        visual_slides = 0
        def _collect(shape) -> None:
            nonlocal n_images
            st = getattr(shape, "shape_type", None)
            if st == 6:  # GROUP: recurse into its members
                for sub in shape.shapes:
                    _collect(sub)
                return
            if st == 13:  # PICTURE
                n_images += 1
            if getattr(shape, "has_text_frame", False):
                for para in shape.text_frame.paragraphs:
                    if para.text:
                        text_parts.append(para.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        if cell.text:
                            text_parts.append(cell.text)

        for slide in prs.slides:
            try:
                visual_slides += _slide_is_visual(slide)
            except Exception:
                visual_slides += 1  # unprofilable slide: assume visual, err high
            for shape in slide.shapes:
                _collect(shape)
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text
                if notes:
                    text_parts.append(notes)
    except Exception:
        # Fallback: direct OOXML zip walk. Handles .ppsx / .potx and
        # any content-type quirks python-pptx rejects.
        import re
        import zipfile

        with zipfile.ZipFile(path) as zf:
            slide_names = sorted(
                n for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            pages = len(slide_names) or 1
            text_pattern = re.compile(r"<a:t[^>]*>([^<]*)</a:t>")
            pic_pattern = re.compile(r"<p:pic\b")
            for name in slide_names:
                content = zf.read(name).decode("utf-8", errors="replace")
                text_parts.extend(text_pattern.findall(content))
                n_images += len(pic_pattern.findall(content))

    body_tokens = count_tokens("\n".join(text_parts))
    n_sig = min(n_images, _media_images(path, "ppt/media/")[1])
    img_low = n_sig * SHORT_TOKENS_LOW  # only described pictures produce billed text
    img_exp = n_sig * SHORT_TOKENS_EXPECTED
    img_high = n_sig * SHORT_TOKENS_HIGH

    band = band_for(
        pages=pages,
        tokens_low=body_tokens + img_low,
        tokens_expected=body_tokens + img_exp,
        tokens_high=body_tokens + img_high,
    )
    detail = {
        "kind": "pptx",
        "pages": pages,
        "slides": pages,
        "visual_slides": visual_slides if visual_slides is not None else pages,  # fallback path: all slides
        "body_tokens": body_tokens,
        "n_images": n_images,
        "n_images_unique": n_images,
        "n_images_significant": n_sig,  # pictures >= 5 KB and >= 100 x 100 px (the ones that get described)
        "tokens_low": body_tokens + img_low,
        "tokens_expected": body_tokens + img_exp,
        "tokens_high": body_tokens + img_high,
    }
    return band, detail


def _estimate_xlsx(path: Path) -> tuple[CreditBand, dict]:
    from openpyxl import load_workbook

    # read_only + data_only: skip formula parsing, materialize only the
    # cached values. Critical for big sheets — full parse can balloon
    # memory and time. Estimate-grade fidelity is fine.
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    text_parts: list[str] = []
    sheet_count = 0
    for sheet in wb.worksheets:
        sheet_count += 1
        text_parts.append(f"# {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            for val in row:
                if val is not None:
                    text_parts.append(str(val))
    body = "\n".join(text_parts)
    body_tokens = count_tokens(body)
    n_all, n_sig = _media_images(path, "xl/media/")
    band = band_for(
        pages=1,
        tokens_low=body_tokens + n_sig * SHORT_TOKENS_LOW,
        tokens_expected=body_tokens + n_sig * SHORT_TOKENS_EXPECTED,
        tokens_high=body_tokens + n_sig * SHORT_TOKENS_HIGH,
    )
    detail = {
        "kind": "xlsx",
        "pages": 1,
        "sheets": sheet_count,
        "body_tokens": body_tokens,
        "n_images_unique": n_all,
        "n_images_significant": n_sig,
        "tokens_low": body_tokens + n_sig * SHORT_TOKENS_LOW,
        "tokens_expected": body_tokens + n_sig * SHORT_TOKENS_EXPECTED,
        "tokens_high": body_tokens + n_sig * SHORT_TOKENS_HIGH,
    }
    return band, detail


def _estimate_ooxml_fast(path: Path, ext: str) -> tuple[CreditBand, dict]:
    """Zip + regex extraction — bypasses python-docx / python-pptx /
    openpyxl entirely. Reads only the XML parts we need.

    DOCX: word/document.xml — count `<w:t>` text + `lastRenderedPageBreak`
    PPTX: ppt/slides/slide*.xml — count `<a:t>` text + slide count
    XLSX: xl/sharedStrings.xml + per-sheet inline strings

    Always faster than the full path; never as accurate (skips header
    text, footnotes, formula cells, etc). For a corpus-scale scan the
    accuracy delta is dwarfed by the credit-formula's max(pages,
    tokens/1000) operation anyway.
    """
    text_parts: list[str] = []
    pages = 1
    n_images = 0

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())

        if ext in ("docx", "dotx"):
            doc_xml_name = "word/document.xml"
            if doc_xml_name in names:
                xml = zf.read(doc_xml_name).decode("utf-8", errors="replace")
                text_parts.extend(_OOXML_TEXT_RE.findall(xml))
                breaks = len(_OOXML_PAGE_BREAK_RE.findall(xml))
                sects = re.findall(r"<w:sectPr\b.*?</w:sectPr>", xml, re.S)
                new_page_sects = sum(
                    1 for sp in sects[1:]
                    if 'w:val="continuous"' not in sp and 'w:val="nextColumn"' not in sp
                )
                explicit = len(_OOXML_EXPLICIT_BREAK_RE.findall(xml)) + new_page_sects + 1
                nchars = len("".join(text_parts))
                heur = max(1, -(-nchars // _CHARS_PER_PAGE_FALLBACK))
                pages, _ = _choose_pages(_app_xml_pages(path), heur, explicit, breaks)
                n_images = len(_OOXML_PIC_RE.findall(xml))

        elif ext in ("pptx", "potx", "ppsx"):
            slide_names = sorted(
                n for n in names
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            pages = len(slide_names) or 1
            for name in slide_names:
                xml = zf.read(name).decode("utf-8", errors="replace")
                text_parts.extend(_OOXML_TEXT_RE.findall(xml))
                n_images += len(_OOXML_PIC_RE.findall(xml))

        elif ext in ("xlsx", "xltx"):
            # Shared strings table (string pool) + every cell value.
            # _XLSX_VALUE_RE catches numeric cells too — critical for
            # financial workbooks where 99% of cells are numbers.
            if "xl/sharedStrings.xml" in names:
                xml = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                text_parts.extend(_OOXML_TEXT_RE.findall(xml))
            for name in names:
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                    xml = zf.read(name).decode("utf-8", errors="replace")
                    text_parts.extend(_XLSX_VALUE_RE.findall(xml))
            pages = 1
        else:
            raise ValueError(f"unsupported office ext in fast path: {ext}")

    raw_tokens = count_tokens("\n".join(text_parts))
    body_tokens = apply_fast_overhead(raw_tokens, ext)
    size_corner = filesize_corner(path.stat().st_size, ext if ext in {"docx", "pptx", "xlsx"} else "docx")

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
        "kind": ext,
        "pages": pages,
        "fast_mode": True,
        "body_tokens": raw_tokens,
        "body_tokens_with_overhead": body_tokens,
        "n_images_total": n_images,
        "n_images_unique": n_images,
        "n_images_significant": _media_images(path, {"docx": "word/", "dotx": "word/", "xlsx": "xl/", "xltx": "xl/"}.get(ext, "ppt/") + "media/")[1],
        "visual_slides": pages if ext in ("pptx", "potx", "ppsx") else None,  # fast path cannot profile: all slides
        "size_corner": size_corner,
        "tokens_low": body_tokens + img_low,
        "tokens_expected": body_tokens + img_exp,
        "tokens_high": body_tokens + img_high,
    }


_MAIN_CT = {
    "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "ppt": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    "xl": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
}
_ALT_CT_RE = re.compile(r'ContentType="[^"]*(?:template|slideshow|macroEnabled)[^"]*\.main\+xml"')


def _write_normalized_ooxml(src: Path, dst: Path) -> None:
    """Copy an OOXML package, rewriting a template/slideshow/macro main-part content type to the plain
    document/presentation/workbook type so the standard parsers accept it."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == "[Content_Types].xml":
                xml = data.decode("utf-8", errors="replace")
                fam = "word" if "/word/" in xml else "ppt" if "/ppt/" in xml else "xl"
                xml = _ALT_CT_RE.sub(f'ContentType="{_MAIN_CT[fam]}"', xml)
                data = xml.encode("utf-8")
            zout.writestr(info, data)


def estimate_office(path: Path, fast: bool = False) -> tuple[CreditBand, dict]:
    ext = path.suffix.lstrip(".").lower()
    if fast:
        try:
            return _estimate_ooxml_fast(path, ext)
        except (zipfile.BadZipFile, KeyError):
            pass  # fall through to full path
    full = {"docx": _estimate_docx, "dotx": _estimate_docx, "docm": _estimate_docx, "dotm": _estimate_docx,
            "pptx": _estimate_pptx, "potx": _estimate_pptx, "ppsx": _estimate_pptx,
            "pptm": _estimate_pptx, "xlsx": _estimate_xlsx, "xltx": _estimate_xlsx, "xlsm": _estimate_xlsx}.get(ext)
    if full is None:
        raise ValueError(f"unsupported office ext: {ext}")
    if ext in ("docx", "pptx", "xlsx"):
        try:
            return full(path)
        except Exception:
            pass
    # python-docx/-pptx/openpyxl refuse template, slideshow and macro content types (.dotx, .potx,
    # .ppsx, .pptm, .xltx, .xlsm, ...) although the payload is the same OOXML. Swap the main part's
    # content type in a temporary copy (what the platform does too) and parse that fully.
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            canon = {"d": "docx", "p": "pptx", "x": "xlsx"}[ext[0]]
            tmp = Path(td) / f"normalized.{canon}"
            _write_normalized_ooxml(path, tmp)
            band, detail = full(tmp)
            detail["kind"] = canon
            return band, detail
    except Exception:
        # last resort: zip + regex reading
        band, detail = _estimate_ooxml_fast(path, {"dotx": "docx", "docm": "docx", "dotm": "docx", "xltx": "xlsx", "xlsm": "xlsx",
                                                   "potx": "pptx", "ppsx": "pptx", "pptm": "pptx"}.get(ext, ext))
        detail["approx"] = True
        return band, detail
