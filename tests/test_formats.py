"""Format edge cases: aliases, templates, encodings, embedded images, archives, protected files."""
import os
import struct
import zipfile

import pytest

from poma_estimate.measures import group_of, measures_for
from poma_estimate.scan import estimate_one


def test_pptx_group_and_table_text_is_counted(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches
    from poma_estimate.estimators.office import estimate_office
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    grp = s.shapes.add_group_shape()
    tb = grp.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    tb.text_frame.text = "grouped words " * 500
    tbl = s.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(4), Inches(2)).table
    tbl.cell(0, 0).text = "table words " * 500
    prs.save(tmp_path / "g.pptx")
    band, d = estimate_office(tmp_path / "g.pptx")
    assert d["body_tokens"] > 1500 and band.expected >= 2


def test_dotx_template_is_read(tmp_path):
    from docx import Document
    doc = Document(); doc.add_paragraph("template words " * 800); doc.save(tmp_path / "t.docx")
    data = zipfile.ZipFile(tmp_path / "t.docx")
    with zipfile.ZipFile(tmp_path / "t.dotx", "w") as z:
        for n in data.namelist():
            b = data.read(n)
            if n == "[Content_Types].xml":
                b = b.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.template.main+xml")
            z.writestr(n, b)
    r = estimate_one(tmp_path / "t.dotx")
    assert r.error is None and r.band.expected >= 2


def test_docx_explicit_page_break_is_a_floor(tmp_path):
    from docx import Document
    doc = Document(); doc.add_paragraph("page one"); doc.add_page_break(); doc.add_paragraph("page two")
    doc.save(tmp_path / "b.docx")
    from poma_estimate.estimators.office import estimate_office
    _, d = estimate_office(tmp_path / "b.docx")
    assert d["pages"] >= 2


def test_svg_with_xml_declaration_stays_an_image(tmp_path):
    p = tmp_path / "a.svg"
    p.write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"></svg>')
    r = estimate_one(p)
    assert r.ext == "svg" and measures_for(r.detail, r.ext, r.size_bytes) == ("image", {"files": 1, "bytes": r.size_bytes, "frames": 1, "large_files": 0, "converted_files": 0})


def test_decoded_image_format_decides_the_group(tmp_path):
    from PIL import Image
    p = tmp_path / "photo.tif"
    Image.new("RGB", (300, 300), (1, 2, 3)).save(p, format="PNG")
    r = estimate_one(p)
    assert r.ext == "png" and measures_for(r.detail, r.ext, r.size_bytes)[0] == "image"


def test_html_tiny_and_md_images(tmp_path):
    h = tmp_path / "t.html"
    h.write_text("<html><body><p>x</p>" + '<img src="t.gif" width="1" height="1">' * 100 + '<img src="big.jpg"></body></html>')
    r = estimate_one(h)
    assert r.detail["n_images_significant"] == 1
    m = tmp_path / "a.md"
    m.write_text("# t\n![a](a.png) text ![b](b.png)\n")
    assert estimate_one(m).detail["n_images_significant"] == 2


def test_aliases_count_like_their_canonical_format(tmp_path):
    d = {"kind": "text", "tokens_expected": 3000}
    assert measures_for(d, "mdown", 10) == measures_for(d, "md", 10) and measures_for(d, "md", 10)[0] == "markdown"
    h = {"kind": "html", "body_tokens": 2000}
    assert measures_for(h, "shtml", 10) == measures_for(h, "html", 10) and measures_for(h, "html", 10)[0] == "html"
    assert measures_for({"kind": "text", "tokens_expected": 9000}, "xsd", 10) == measures_for({"kind": "text", "tokens_expected": 9000}, "xml", 10)


def test_epub_breaks_counted_once():
    from poma_estimate.estimators.epub import _BREAK_RE
    css = b"<p style='page-break-before: always'>a</p>" * 10
    assert len(_BREAK_RE.findall(css)) == 10


def test_truncated_zip_directory_is_not_listed(tmp_path):
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as z:
        for i in range(20):
            z.writestr(f"f{i}.txt", "x" * 100)
    data = bytearray(p.read_bytes())
    i = data.rfind(b"PK\x05\x06")
    cd_size = struct.unpack("<I", data[i + 12:i + 16])[0]
    data[i + 12:i + 16] = struct.pack("<I", 60)  # directory claims to end after the first record
    p.write_bytes(bytes(data))
    d = estimate_one(p).detail
    assert d["archive_toc"] in ("truncated", "unreadable") and d["contents_from"] == "size guess"


def test_unmeasured_tiff_falls_back_on_size():
    from poma_estimate.measures import size_only
    assert group_of("tiff", None) == "image"
    assert size_only("tiff", None, 10**8) == ("image", {"files": 1, "bytes": 10**8, "unmeasured_files": 1, "unmeasured_bytes": 10**8})


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="POSIX permissions only")
def test_unreadable_folder_warns_on_stderr(tmp_path):
    import subprocess, sys
    locked = tmp_path / "locked"; locked.mkdir(); (locked / "a.txt").write_text("x" * 1000)
    locked.chmod(0)
    try:
        out = subprocess.run([sys.executable, "-m", "poma_estimate", str(tmp_path)], capture_output=True, text=True, timeout=120)
    finally:
        locked.chmod(0o755)
    assert out.stdout.startswith("units ") and "could not be read" in out.stderr


def test_xhtml_with_xml_declaration_is_html(tmp_path):
    p = tmp_path / "a.xhtml"
    p.write_text('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>' + "word " * 2000 + "</p></body></html>")
    r = estimate_one(p)
    assert r.detail["kind"] == "html" and measures_for(r.detail, r.ext, r.size_bytes)[1]["tokens"] > 1000


def test_json_is_ascii_safe(tmp_path):
    import json, subprocess, sys
    (tmp_path / "資料.txt").write_text("hello " * 50)
    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    out = subprocess.run([sys.executable, "-m", "poma_estimate", str(tmp_path), "--json"], capture_output=True, env=env, timeout=120)
    assert out.returncode == 0 and json.loads(out.stdout)["files_counted"] == 1


def test_powerpoint_aliases_count_the_same(tmp_path):
    from pptx import Presentation
    prs = Presentation()
    for i in range(10):
        s = prs.slides.add_slide(prs.slide_layouts[1]); s.shapes.title.text = f"T{i}"; s.placeholders[1].text = "bullet text " * 20
    prs.save(tmp_path / "a.pptx")
    z = zipfile.ZipFile(tmp_path / "a.pptx")
    for ext, ct in (("potx", b"presentationml.template.main+xml"), ("pptm", b"application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml")):
        with zipfile.ZipFile(tmp_path / f"a.{ext}", "w") as out:
            for n in z.namelist():
                b = z.read(n)
                if n == "[Content_Types].xml":
                    b = b.replace(b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", ct if ct.startswith(b"application") else b"application/vnd.openxmlformats-officedocument." + ct)
                out.writestr(n, b)
    base = estimate_one(tmp_path / "a.pptx")
    for ext in ("potx", "pptm"):
        r = estimate_one(tmp_path / f"a.{ext}")
        assert r.detail["visual_slides"] == base.detail["visual_slides"]
        assert measures_for(r.detail, r.ext, 0) == measures_for(base.detail, base.ext, 0)


def test_docx_footnotes_are_counted(tmp_path):
    from docx import Document
    doc = Document(); doc.add_paragraph("short body"); doc.save(tmp_path / "f.docx")
    src = zipfile.ZipFile(tmp_path / "f.docx")
    foot = ('<?xml version="1.0"?><w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:footnote w:id="1"><w:p><w:r><w:t>' + "footnote words " * 2000 + "</w:t></w:r></w:p></w:footnote></w:footnotes>")
    with zipfile.ZipFile(tmp_path / "g.docx", "w") as out:
        for n in src.namelist():
            out.writestr(n, src.read(n))
        out.writestr("word/footnotes.xml", foot)
    r = estimate_one(tmp_path / "g.docx")
    assert r.detail["body_tokens"] > 3000 and r.band.expected >= 4


def test_markdown_tiny_img_tags_and_animated_png(tmp_path):
    m = tmp_path / "a.md"
    m.write_text('<img src="p.gif" width="1" height="1">' * 100 + '\n![x](y.png)\n')
    assert estimate_one(m).detail["n_images_significant"] == 1
    anim = {"kind": "image", "n_frames": 10, "size_bytes": 100_000}
    assert measures_for(anim, "png", 100_000)[1]["frames"] == 1  # animated PNG: first frame only
    assert measures_for(anim, "webp", 100_000)[1]["converted_files"] == 1  # counted as a format conversion


def test_utf16_text_is_decoded(tmp_path):
    a = tmp_path / "a.txt"; b = tmp_path / "b.txt"
    a.write_text("hello world " * 3000, encoding="utf-8")
    b.write_text("hello world " * 3000, encoding="utf-16")
    ta = estimate_one(a).detail["tokens_expected"]; tb = estimate_one(b).detail["tokens_expected"]
    assert abs(tb - ta) <= 3


def test_markdown_inline_base64_images(tmp_path):
    import base64, io as _io
    from PIL import Image
    buf = _io.BytesIO(); Image.frombytes("RGB", (300, 300), os.urandom(270000)).save(buf, "PNG")
    big = base64.b64encode(buf.getvalue()).decode()
    buf = _io.BytesIO(); Image.new("RGB", (1, 1)).save(buf, "PNG"); tiny = base64.b64encode(buf.getvalue()).decode()
    m = tmp_path / "a.md"
    m.write_text(f"# t\n![big](data:image/png;base64,{big})\n" + f"![t](data:image/png;base64,{tiny})\n" * 50 + "text")
    d = estimate_one(m).detail
    assert d["n_images_significant"] == 1 and d["body_tokens"] < 100  # payload is not tokenized


def test_rough_estimate_aliases_match(tmp_path):
    from poma_estimate.progressive import rough_units
    assert rough_units("ppsx", 12_288_000) == rough_units("pptx", 12_288_000)
    from poma_estimate.measures import group_of
    assert group_of("potx", "office") == group_of("pptx", "office") == "powerpoint"


def test_dotx_keeps_page_break_before(tmp_path):
    from docx import Document
    doc = Document()
    for i in range(5):
        doc.add_paragraph(f"para {i}").paragraph_format.page_break_before = i > 0
    doc.save(tmp_path / "t.docx")
    src = zipfile.ZipFile(tmp_path / "t.docx")
    with zipfile.ZipFile(tmp_path / "t.dotx", "w") as z:
        for n in src.namelist():
            b = src.read(n)
            if n == "[Content_Types].xml":
                b = b.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.template.main+xml")
            z.writestr(n, b)
    assert estimate_one(tmp_path / "t.dotx").detail["pages"] == estimate_one(tmp_path / "t.docx").detail["pages"] >= 5


def _png_bytes(w=300, h=300):
    import io as _io
    from PIL import Image
    buf = _io.BytesIO(); Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)).save(buf, "PNG"); return buf.getvalue()


def test_large_markdown_does_not_tokenize_base64(tmp_path, monkeypatch):
    import base64
    from poma_estimate.estimators import text as text_mod
    monkeypatch.setattr(text_mod, "_SAMPLE_OVER", 1_000_000)  # force the sampled path on a small file
    b64 = base64.b64encode(_png_bytes(600, 600)).decode()
    m = tmp_path / "big.md"
    m.write_text("# t\nsome text\n" + f"![x](data:image/png;base64,{b64})\n" * 2)
    assert estimate_one(m).band.expected < 50


def test_markdown_and_xlsx_images_enter_billing(tmp_path):
    import base64
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    pngs = [_png_bytes() for _ in range(20)]
    m = tmp_path / "a.md"
    m.write_text("".join(f"![i](data:image/png;base64,{base64.b64encode(p).decode()})\n" for p in pngs))
    r = estimate_one(m)
    assert r.detail["n_images_significant"] == 20 and r.band.expected >= 2
    wb = Workbook(); ws = wb.active; ws["A1"] = "x"
    for i, p in enumerate(pngs):
        f = tmp_path / f"i{i}.png"; f.write_bytes(p); ws.add_image(XLImage(str(f)), f"C{3 + i * 20}")
    wb.save(tmp_path / "s.xlsx")
    r = estimate_one(tmp_path / "s.xlsx")
    assert r.detail["n_images_significant"] == 20 and r.band.expected >= 2


def test_docx_icons_do_not_add_units_and_nested_tables_count(tmp_path):
    from docx import Document
    from docx.shared import Inches
    doc = Document()
    for i in range(20):
        f = tmp_path / f"ic{i}.png"; f.write_bytes(_png_bytes(10, 10)); doc.add_picture(str(f), width=Inches(0.1))
    outer = doc.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = "nested words " * 2000
    doc.save(tmp_path / "d.docx")
    r = estimate_one(tmp_path / "d.docx")
    assert r.detail["n_images_significant"] == 0 and r.detail["body_tokens"] > 3000


def test_mhtml_is_decoded(tmp_path):
    import base64
    png = base64.encodebytes(_png_bytes()).decode()
    mht = ("MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"B\"\r\n\r\n--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           "<html><body><p>hello</p><img src=\"cid:img1\"></body></html>\r\n--B\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
           "Content-ID: <img1>\r\n\r\n" + png + "\r\n--B--\r\n")
    p = tmp_path / "page.mhtml"; p.write_bytes(mht.encode())  # bytes: text mode would double CRs on Windows
    r = estimate_one(p)
    assert r.band.expected <= 2 and r.detail["n_images_significant"] == 1


def test_pdf_images_inside_forms_count(tmp_path):
    from PIL import Image
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject
    from poma_estimate.estimators.pdf import _significant_images
    img_pdf = tmp_path / "img.pdf"
    Image.new("RGB", (600, 800), (9, 9, 9)).save(img_pdf, "PDF", resolution=72)
    w = PdfWriter(clone_from=str(img_pdf))
    page = w.pages[0]
    inner_res = page["/Resources"]
    content = page.get_contents().get_data()
    res = inner_res
    for level in range(3):  # wrap the page content in three nested form XObjects
        form = StreamObject()
        form._data = content
        form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                     NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(600), NumberObject(800)]),
                     NameObject("/Resources"): res})
        ref = w._add_object(form)
        res = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject(f"/F{level}"): ref})})
        content = f"/F{level} Do".encode()
    page[NameObject("/Resources")] = res
    new = StreamObject(); new._data = content
    page[NameObject("/Contents")] = w._add_object(new)
    out = tmp_path / "forms.pdf"
    with open(out, "wb") as fh:
        w.write(fh)
    assert _significant_images(img_pdf.read_bytes())[0] == 1
    assert _significant_images(out.read_bytes())[0] == 1


def test_docx_large_table_keeps_all_text(tmp_path):
    from docx import Document
    doc = Document()
    t = doc.add_table(rows=100, cols=2)
    for i in range(100):
        for j in range(2):
            t.cell(i, j).text = f"row t {i} {j} " + "word " * 100
    doc.save(tmp_path / "t.docx")
    assert estimate_one(tmp_path / "t.docx").detail["body_tokens"] > 18000


def test_sampled_markdown_keeps_long_alphanumeric_text(tmp_path, monkeypatch):
    from poma_estimate.estimators import text as text_mod
    monkeypatch.setattr(text_mod, "_SAMPLE_OVER", 1_000_000)
    m = tmp_path / "digits.md"
    m.write_text("```\n" + "1234567890" * 300_000 + "\n```\n")
    assert estimate_one(m).band.expected > 100


def test_dotm_is_discovered(tmp_path):
    from docx import Document
    doc = Document(); doc.add_paragraph("macro template words " * 800); doc.save(tmp_path / "t.docx")
    src = zipfile.ZipFile(tmp_path / "t.docx")
    with zipfile.ZipFile(tmp_path / "t.dotm", "w") as z:
        for n in src.namelist():
            b = src.read(n)
            if n == "[Content_Types].xml":
                b = b.replace(b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                              b"application/vnd.ms-word.template.macroEnabledTemplate.main+xml")
            z.writestr(n, b)
    r = estimate_one(tmp_path / "t.dotm")
    assert r.error is None and r.band.expected >= 2


def test_mhtml_content_location_image_counted_once(tmp_path):
    import base64
    png = base64.encodebytes(_png_bytes()).decode()
    mht = ("MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"B\"\r\n\r\n--B\r\nContent-Type: text/html\r\n\r\n"
           "<html><body><p>hi</p><img src=\"https://example.invalid/image.png\"></body></html>\r\n--B\r\nContent-Type: image/png\r\n"
           "Content-Transfer-Encoding: base64\r\nContent-Location: https://example.invalid/image.png\r\n\r\n" + png + "\r\n--B--\r\n")
    p = tmp_path / "p.mhtml"; p.write_bytes(mht.encode())
    assert estimate_one(p).detail["n_images_significant"] == 1


def test_pdf_form_placed_twice_counts_large_placement(tmp_path):
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject
    from poma_estimate.estimators.pdf import _significant_images
    img_pdf = tmp_path / "img.pdf"
    Image.new("RGB", (600, 800), (9, 9, 9)).save(img_pdf, "PDF", resolution=72)
    w = PdfWriter(clone_from=str(img_pdf))
    page = w.pages[0]
    form = StreamObject(); form._data = page.get_contents().get_data()
    form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                 NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(600), NumberObject(800)]),
                 NameObject("/Resources"): page["/Resources"]})
    ref = w._add_object(form)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/F0"): ref})})
    c = StreamObject(); c._data = b"q 0.01 0 0 0.01 0 0 cm /F0 Do Q q /F0 Do Q"
    page[NameObject("/Contents")] = w._add_object(c)
    out = tmp_path / "twice.pdf"
    with open(out, "wb") as fh:
        w.write(fh)
    assert _significant_images(out.read_bytes())[0] == 1


def test_docx_content_control_text(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml
    doc = Document(); doc.add_paragraph("intro")
    sdt = parse_xml('<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:sdtContent><w:p><w:r><w:t>'
                    + "content control words " * 2000 + "</w:t></w:r></w:p></w:sdtContent></w:sdt>")
    doc.element.body.insert(0, sdt)
    doc.save(tmp_path / "c.docx")
    assert estimate_one(tmp_path / "c.docx").detail["body_tokens"] > 5000


def test_cp1252_html_not_read_as_utf16(tmp_path):
    p = tmp_path / "w.html"
    body = '<html><head><meta charset="windows-1252"></head><body>' + "<p>caf\xe9 hello world</p>" * 2000 + "</body></html>"
    data = body.encode("cp1252")
    if len(data) % 2:
        data += b" "
    p.write_bytes(data)
    assert estimate_one(p).detail["body_tokens"] < 20000


def test_ods_repeated_rows(tmp_path):
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P
    from poma_estimate.estimators.opendoc import estimate_opendoc
    ods = OpenDocumentSpreadsheet(); t = Table(name="S")
    r = TableRow(numberrowsrepeated=2000); c = TableCell(); c.addElement(P(text="repeated row text")); r.addElement(c); t.addElement(r)
    ods.spreadsheet.addElement(t); ods.save(str(tmp_path / "r.ods"))
    _, d = estimate_opendoc(tmp_path / "r.ods")
    assert d["body_tokens"] > 5000


def test_sampled_markdown_counts_linked_images(tmp_path, monkeypatch):
    from poma_estimate.estimators import text as text_mod
    monkeypatch.setattr(text_mod, "_SAMPLE_OVER", 1_000_000)
    m = tmp_path / "links.md"
    m.write_text("".join(f"![d](https://example.invalid/d-{i}.png)\n" for i in range(40_000)))
    assert estimate_one(m).detail["n_images_significant"] == 40_000


def test_epub_paginates_each_chapter(tmp_path):
    from ebooklib import epub
    from poma_estimate.estimators.epub import estimate_epub
    book = epub.EpubBook(); book.set_identifier("x"); book.set_title("t"); book.set_language("en")
    chs = [epub.EpubHtml(title="long", file_name="long.xhtml", content="<html><body><p>" + "abcdefghi " * 30_000 + "</p></body></html>")]
    chs += [epub.EpubHtml(title=f"s{i}", file_name=f"s{i}.xhtml", content="<html><body><p>short</p></body></html>") for i in range(100)]
    for c in chs:
        book.add_item(c)
    book.spine = chs; book.add_item(epub.EpubNcx()); book.add_item(epub.EpubNav())
    epub.write_epub(str(tmp_path / "b.epub"), book)
    _, d = estimate_epub(tmp_path / "b.epub")
    assert d["pages"] >= 195


def test_docx_text_box_counted_once(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml
    doc = Document(); doc.add_paragraph("body")
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    box = parse_xml(f'<w:p xmlns:w="{W}" xmlns:v="urn:schemas-microsoft-com:vml"><w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
                    f'<w:p><w:r><w:t>{"unique textbox words " * 2000}</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>')
    doc.element.body.insert(0, box)
    doc.save(tmp_path / "tb.docx")
    t = estimate_one(tmp_path / "tb.docx").detail["body_tokens"]
    assert 5500 < t < 7000


def test_password_protected_pdf_counted_by_size_only(tmp_path):
    from pypdf import PdfWriter
    from poma_estimate.progressive import scan_progressive
    w = PdfWriter()
    for _ in range(12):
        w.add_blank_page(width=600, height=800)
    w.encrypt(user_password="secret", owner_password="owner")
    with open(tmp_path / "locked.pdf", "wb") as fh:
        w.write(fh)
    report, est = scan_progressive(tmp_path)
    r = report.results[0]
    assert r.error  # reported as unreadable, not as invented pages/tokens
    c = est.counts["pdf"]
    assert c["files"] == 1 and c["unmeasured_files"] == 1 and c.get("pages", 0) == 0
    assert est.expected >= 1  # units from the size guess, not 0
    import json
    from poma_estimate.output import render_json
    d = json.loads(render_json(report, est, tmp_path))
    f = d["files"][0]
    assert f["error"] and f["counts"] == {"files": 1, "bytes": r.size_bytes, "unmeasured_files": 1, "unmeasured_bytes": r.size_bytes}
    assert f["units"] >= 1 and d["counts"]["pdf"]["unmeasured_files"] == 1
