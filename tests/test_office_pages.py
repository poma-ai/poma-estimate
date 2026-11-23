from docx import Document
from docx.enum.section import WD_SECTION

from poma_estimate.estimators.office import estimate_office


def test_disabled_page_break_before_does_not_count(tmp_path):
    d = Document()
    for i in range(20):
        d.add_paragraph(f"short {i}").paragraph_format.page_break_before = False
    p = tmp_path / "a.docx"
    d.save(p)
    band, det = estimate_office(p)
    assert det["pages"] == 1


def test_section_page_breaks_count(tmp_path):
    d = Document()
    for i in range(10):
        d.add_paragraph(f"form {i}")
        d.add_section(WD_SECTION.NEW_PAGE)
    p = tmp_path / "b.docx"
    d.save(p)
    band, det = estimate_office(p)
    assert det["pages"] >= 10


def test_cli_help_exits_zero():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "poma_estimate", "--help"], capture_output=True)
    assert r.returncode == 0


def test_huge_saved_page_count_is_ignored(tmp_path):
    import zipfile, shutil
    d = Document()
    d.add_paragraph("one short paragraph")
    p = tmp_path / "c.docx"
    d.save(p)
    out = tmp_path / "d.docx"
    with zipfile.ZipFile(p) as zi, zipfile.ZipFile(out, "w") as zo:
        for it in zi.infolist():
            data = zi.read(it.filename)
            if it.filename == "docProps/app.xml":
                import re
                data = re.sub(rb"<Pages>\d+</Pages>", b"<Pages>999999</Pages>", data) if b"<Pages>" in data else data.replace(b"</Properties>", b"<Pages>999999</Pages></Properties>")
            zo.writestr(it, data)
    assert estimate_office(out)[1]["pages"] == 1
    from poma_estimate.estimators.office import _estimate_ooxml_fast
    assert _estimate_ooxml_fast(out, "docx")[1]["pages"] == 1


def test_html_poster_data_uri_not_tokenized(tmp_path):
    from poma_estimate.estimators.html_doc import estimate_html
    p = tmp_path / "v.html"
    p.write_text('<video src="m.mp4" poster="data:image/png;base64,' + "A" * 200_000 + '">Video</video><p>hi</p>')
    assert estimate_html(p)[1]["body_tokens"] < 50


def test_new_page_sections_count_in_both_paths(tmp_path):
    from poma_estimate.estimators.office import _estimate_ooxml_fast
    d = Document()
    for i in range(20):
        d.add_paragraph(f"form {i}")
        d.add_section(WD_SECTION.NEW_PAGE)
    p = tmp_path / "e.docx"
    d.save(p)
    assert 20 <= estimate_office(p)[1]["pages"] <= 21
    assert 20 <= _estimate_ooxml_fast(p, "docx")[1]["pages"] <= 21
