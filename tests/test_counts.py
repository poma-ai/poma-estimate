import pytest

from poma_estimate.measures import measures_for


def test_pdf_counts():
    d = {"kind": "pdf", "pages": 10, "body_tokens_extracted": 8000, "tokens_expected": 60000,
         "ocr_pages_est": 2, "n_images_significant": 3, "n_images_unique": 40}
    g, m = measures_for(d, "pdf", 1000)
    assert g == "pdf" and (m["pages"], m["scanned_pages"], m["scanned_files"], m["tokens"], m["images"]) == (10, 2, 0, 8000, 3)
    scan = {"kind": "pdf", "pages": 4, "scanned": True, "body_tokens_extracted": 0, "ocr_pages_est": 4, "n_images_significant": 4}
    g, m = measures_for(scan, "pdf", 1000)
    assert g == "pdf" and (m["pages"], m["scanned_pages"], m["scanned_files"], m["tokens"], m["images"]) == (4, 4, 1, 0, 4)
    no_est = {"kind": "pdf", "pages": 3, "scanned": True, "body_tokens_extracted": 0}
    assert measures_for(no_est, "pdf", 1)[1]["scanned_pages"] == 3  # without an estimate, every page of a scan


def test_office_counts():
    g, m = measures_for({"kind": "docx", "pages": 3, "body_tokens": 7000, "n_images_significant": 2}, "docx", 0)
    assert g == "word" and (m["pages"], m["tokens"], m["images"], m["converted_files"]) == (3, 7000, 2, 0)
    g, m = measures_for({"kind": "xlsx", "pages": 1, "body_tokens": 376000, "n_images_significant": 7}, "xlsx", 0)
    assert g == "excel" and (m["tokens"], m["images"], m["converted_files"]) == (376000, 7, 0) and "pages" not in m
    g, m = measures_for({"kind": "pptx", "pages": 20, "visual_slides": 12, "body_tokens": 3000}, "pptx", 0)
    assert g == "powerpoint" and (m["slides"], m["visual_slides"], m["files_with_visual_slides"], m["tokens"]) == (20, 12, 1, 3000)
    g, m = measures_for({"kind": "pptx", "pages": 5, "visual_slides": 0, "body_tokens": 1000}, "pptx", 0)
    assert (m["slides"], m["visual_slides"], m["files_with_visual_slides"]) == (5, 0, 0)
    assert measures_for({"kind": "doc", "pages": 1, "body_tokens": 1000}, "doc", 0) == ("word", {"files": 1, "bytes": 0, "pages": 1, "tokens": 1000, "images": 0, "converted_files": 0})
    assert measures_for({"kind": "pages", "pages": 1, "body_tokens": 1000}, "pages", 0)[1]["converted_files"] == 1
    for ext in ("ppt", "key", "odp"):
        assert measures_for({"kind": "pptx", "pages": 2, "visual_slides": 1}, ext, 0)[1]["converted_files"] == 1


def test_text_and_markup_counts():
    assert measures_for({"kind": "text", "tokens_expected": 2000}, "md", 0) == ("markdown", {"files": 1, "bytes": 0, "tokens": 2000, "images": 0})
    assert measures_for({"kind": "text", "tokens_expected": 2000}, "txt", 0) == ("text", {"files": 1, "bytes": 0, "tokens": 2000})
    assert measures_for({"kind": "html", "body_tokens": 2000, "n_images_significant": 3}, "html", 0) == ("html", {"files": 1, "bytes": 0, "tokens": 2000, "images": 3})
    assert measures_for({"kind": "text", "tokens_expected": 9000}, "xml", 0) == ("xml", {"files": 1, "bytes": 0, "tokens": 9000})


def test_image_and_converted_document_counts():
    img = lambda size, ext, **kw: measures_for({"kind": "image", "pages": 1, "size_bytes": size, **kw}, ext, size)
    assert img(200_000, "png") == ("image", {"files": 1, "bytes": 200_000, "frames": 1, "large_files": 0, "converted_files": 0})
    assert img(3_000_000, "jpg")[1]["large_files"] == 1  # 1 MB or more
    assert img(200_000, "heic")[1]["converted_files"] == 1
    assert img(100, "gif", n_frames=207)[1]["frames"] == 6  # animated GIF: at most 6 key frames
    g, m = measures_for({"kind": "image", "pages": 5, "n_frames": 5}, "tiff", 0)
    assert g == "converted_docs" and (m["pages"], m["scanned_pages"], m["images"], m["converted_files"]) == (5, 5, 5, 1)
    g, m = measures_for({"kind": "epub", "pages": 4, "body_tokens": 9000}, "epub", 0)
    assert g == "converted_docs" and (m["pages"], m["scanned_pages"], m["tokens"], m["converted_files"]) == (4, 0, 9000, 1)


def test_media_and_size_only_counts():
    assert measures_for({"kind": "audio", "minutes": 3.0}, "mp3", 0) == ("audio", {"files": 1, "bytes": 0, "minutes": 3.0})
    assert measures_for({"kind": "video", "minutes": 61.2}, "mp4", 0) == ("video", {"files": 1, "bytes": 0, "minutes": 61.2})


def test_totals_describe_as_plain_count_lines():
    from poma_estimate.measures import add, add_unmeasured, describe, empty_totals, rounded
    t = empty_totals()
    add(t, *measures_for({"kind": "pdf", "pages": 3, "body_tokens_extracted": 2687, "n_images_significant": 1}, "pdf", 182_000))
    add(t, *measures_for({"kind": "pdf", "pages": 4, "scanned": True, "body_tokens_extracted": 0, "ocr_pages_est": 4}, "pdf", 1_000))
    add_unmeasured(t, "pdf", 1, 50_000)
    add(t, *measures_for({"kind": "video", "minutes": 20.0}, "mp4", 0))
    lines = describe(rounded(t))
    pdf = next(l for l in lines if l.startswith("PDF: "))
    assert pdf.startswith("PDF: 3 files, ") and "7 pages" in pdf and "4 pages without a text layer" in pdf and "1 fully scanned file" in pdf
    assert "2,687 text tokens" in pdf and "1 image" in pdf
    assert any(l.startswith("Video: 1 file, ") and "20 minutes" in l for l in lines)


def test_pdf_ocr_pages_follow_presplit_of_each_scanned_page(tmp_path):
    from pypdf import PdfWriter
    from poma_estimate.estimators.pdf import estimate_pdf
    w = PdfWriter()
    w.add_blank_page(width=600, height=10000)
    w.add_blank_page(width=600, height=800)
    f = tmp_path / "scan.pdf"
    with open(f, "wb") as fh:
        w.write(fh)
    band, d = estimate_pdf(f)
    assert d["pages"] == 6 and d["ocr_pages_est"] == 6


def test_pdf_counts_images_drawn_large_enough(tmp_path):
    from PIL import Image
    from pypdf import PdfReader, PdfWriter
    from poma_estimate.estimators.pdf import estimate_pdf
    big = tmp_path / "big.pdf"
    Image.new("RGB", (800, 1000), (200, 10, 10)).save(big, "PDF", resolution=72)  # the image is the whole page
    _, d = estimate_pdf(big)
    assert d["n_images_significant"] == 1


def test_tiff_frames_are_read_from_the_file(tmp_path):
    from PIL import Image
    from poma_estimate.estimators.image import estimate_image
    frames = [Image.new("L", (300, 300), 255 - 10 * i) for i in range(4)]
    f = tmp_path / "multi.tiff"
    frames[0].save(f, save_all=True, append_images=frames[1:])
    band, d = estimate_image(f)
    assert d["n_frames"] == 4 and d["pages"] == 4 and band.expected >= 4  # multi-page TIFF bills each page


def test_damaged_multiframe_image_keeps_its_unit(tmp_path):
    from PIL import Image
    from poma_estimate.estimators.image import estimate_image
    frames = [Image.new("P", (120, 120), i * 40) for i in range(3)]
    f = tmp_path / "anim.gif"
    frames[0].save(f, save_all=True, append_images=frames[1:], duration=100)
    data = f.read_bytes()
    f.write_bytes(data[: len(data) * 3 // 5])  # truncate inside the frame table
    band, d = estimate_image(f)
    assert d["pages"] == 1 and band.expected >= 1 and d["n_frames"] >= 1


def test_opendoc_spreadsheet_and_presentation_are_read(tmp_path):
    from odf.opendocument import OpenDocumentPresentation, OpenDocumentSpreadsheet
    from odf.draw import Frame, Page, TextBox
    from odf.style import MasterPage, PageLayout
    from odf.table import Table, TableCell, TableRow
    from odf.text import P
    from poma_estimate.estimators.opendoc import estimate_opendoc
    ods = OpenDocumentSpreadsheet()
    t = Table(name="S")
    for i in range(50):
        r = TableRow(); c = TableCell(); c.addElement(P(text=f"row {i} " + "word " * 40)); r.addElement(c); t.addElement(r)
    ods.spreadsheet.addElement(t)
    ods.save(str(tmp_path / "s.ods"))
    band, d = estimate_opendoc(tmp_path / "s.ods")
    assert d["body_tokens"] > 1000 and band.expected >= 2
    odp = OpenDocumentPresentation()
    pl = PageLayout(name="L"); odp.automaticstyles.addElement(pl)
    mp = MasterPage(name="M", pagelayoutname=pl); odp.masterstyles.addElement(mp)
    pg = Page(masterpagename=mp); fr = Frame(width="10cm", height="5cm", x="1cm", y="1cm"); tb = TextBox()
    tb.addElement(P(text="slide text " * 30)); fr.addElement(tb); pg.addElement(fr); odp.presentation.addElement(pg)
    odp.save(str(tmp_path / "p.odp"))
    band, d = estimate_opendoc(tmp_path / "p.odp")
    assert d["body_tokens"] > 30 and d["pages"] == 1


def test_tiff_named_jpg_is_still_billed_per_frame(tmp_path):
    from PIL import Image
    from poma_estimate.scan import estimate_one
    frames = [Image.new("L", (300, 300), 255 - 10 * i) for i in range(5)]
    f = tmp_path / "scan.jpg"
    frames[0].save(f, format="TIFF", save_all=True, append_images=frames[1:])
    r = estimate_one(f)
    assert r.detail["pages"] == 5 and r.band.expected >= 5 and r.ext == "tiff"
    g, m = measures_for(r.detail, r.ext, r.size_bytes)
    assert g == "converted_docs" and (m["pages"], m["scanned_pages"], m["images"], m["converted_files"]) == (5, 5, 5, 1)


def test_epub_follows_spine_and_page_breaks(tmp_path):
    from ebooklib import epub
    from poma_estimate.estimators.epub import estimate_epub
    book = epub.EpubBook(); book.set_identifier("x"); book.set_title("t"); book.set_language("en")
    ch = epub.EpubHtml(title="c", file_name="c.xhtml",
                       content="<html><body>" + "<p style='page-break-after: always'>p</p>" * 100 + "</body></html>")
    book.add_item(ch)
    for i in range(50):  # documents outside the spine are not rendered
        book.add_item(epub.EpubHtml(title=f"x{i}", file_name=f"x{i}.xhtml", content="<html><body><p>x</p></body></html>"))
    book.spine = [ch]; book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
    f = tmp_path / "b.epub"
    epub.write_epub(str(f), book)
    band, d = estimate_epub(f)
    assert d["chapters"] == 1 and d["pages"] >= 100 and d["approx"] is True


def _noise_png(path, w, h):
    import os
    from PIL import Image
    Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(path)


def test_docx_and_xlsx_count_significant_images(tmp_path):
    from docx import Document
    from docx.shared import Inches
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from poma_estimate.estimators.office import estimate_office
    _noise_png(tmp_path / "big.png", 300, 300)   # >= 100x100 px and >= 5 KB
    _noise_png(tmp_path / "tiny.png", 20, 20)    # a bullet-sized glyph
    doc = Document(); doc.add_paragraph("hello")
    doc.add_picture(str(tmp_path / "big.png"), width=Inches(2)); doc.add_picture(str(tmp_path / "tiny.png"), width=Inches(0.2))
    doc.save(tmp_path / "d.docx")
    _, d = estimate_office(tmp_path / "d.docx")
    assert d["n_images_significant"] == 1
    wb = Workbook(); ws = wb.active; ws["A1"] = "x"; ws.add_image(XLImage(str(tmp_path / "big.png")), "C3"); wb.save(tmp_path / "s.xlsx")
    _, d = estimate_office(tmp_path / "s.xlsx")
    assert d["n_images_significant"] == 1 and measures_for(d, "xlsx", 0)[1]["images"] == 1


def test_pptx_visual_slides_follow_the_platform_rule(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    from poma_estimate.estimators.office import estimate_office
    _noise_png(tmp_path / "big.png", 300, 300)
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])          # title + bullets: programmatic
    s1.shapes.title.text = "Agenda"; s1.placeholders[1].text = "one\ntwo\nthree, plenty of text here"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])          # title + picture: visual
    s2.shapes.title.text = "Photo"; s2.shapes.add_picture(str(tmp_path / "big.png"), Inches(1), Inches(1))
    prs.slides.add_slide(prs.slide_layouts[6])               # empty: visual
    prs.save(tmp_path / "p.pptx")
    _, d = estimate_office(tmp_path / "p.pptx")
    assert d["slides"] == 3 and d["visual_slides"] == 2
