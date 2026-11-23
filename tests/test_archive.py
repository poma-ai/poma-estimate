import io
import tarfile
import zipfile

from poma_estimate.measures import measures_for
from poma_estimate.scan import estimate_one


def _zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)


def test_zip_counts_its_contents_and_lists_them(tmp_path):
    p = tmp_path / "bundle.zip"
    _zip(p, {"a.txt": "word " * 4000, "docs/b.md": "x" * 8192, "__MACOSX/._a.txt": "x",
             "inner.zip": b"PK\x05\x06" + b"\0" * 18, "c.unknownext": "x"})
    r = estimate_one(p)
    assert r.error is None
    d = r.detail
    assert r.band.expected == max(1, round(d["unpacked_units_est"]))  # contents, not the container
    assert d["kind"] == "archive" and d["archive_toc"] == "listed"
    assert d["archive_supported_members"] == 3  # a.txt, b.md and the nested inner.zip (counted by its size)
    assert d["archive_nested_archives"] == 1
    assert d["unpacked_units_est"] >= 2 and d["archive_supported_bytes"] > 0
    g, m = measures_for(d, r.ext, r.size_bytes)
    assert g == "archive" and (m["files"], m["bytes"], m["contained_files"]) == (1, r.size_bytes, 3) and m["contained_bytes"] > 0


def test_zip_bomb_is_never_decompressed(tmp_path):
    p = tmp_path / "bomb.zip"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", b"\0" * 50_000_000)  # 50 MB of zeros, ~50 KB compressed
    r = estimate_one(p)
    assert r.detail["archive_member_bytes"] == 50_000_000  # read from the directory, not by inflating
    assert r.band.expected > 1000  # 50 MB of text, as declared


def test_tar_is_counted_but_not_listed(tmp_path):
    data = b"hello " * 1000
    for name, mode in (("x.tar", "w"), ("x.tar.gz", "w:gz")):
        with tarfile.open(tmp_path / name, mode) as t:
            ti = tarfile.TarInfo("notes.txt"); ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        r = estimate_one(tmp_path / name)
        assert r.detail["archive_toc"] == "not listable" and r.error is None
        assert r.detail["contents_from"] == "size guess" and r.band.expected >= 1


def test_zip_with_too_many_entries_is_not_parsed(tmp_path, monkeypatch):
    from poma_estimate.estimators import archive
    p = tmp_path / "many.zip"
    _zip(p, {f"f{i}.txt": "x" for i in range(50)})
    monkeypatch.setattr(archive, "MAX_MEMBERS", 10)
    r = estimate_one(p)
    assert r.detail["archive_toc"] == "too large to list" and r.detail["archive_declared_entries"] == 50


def test_zip_lying_about_its_directory_is_bounded(tmp_path):
    import struct
    p = tmp_path / "liar.zip"
    _zip(p, {"a.txt": "x" * 100})
    data = bytearray(p.read_bytes())
    i = data.rfind(b"PK\x05\x06")
    data[i + 10:i + 12] = struct.pack("<H", 0xFFFF)  # ZIP64 marker in the plain record
    p.write_bytes(bytes(data))
    assert estimate_one(p).detail["archive_toc"] == "too large to list"
    data[i + 10:i + 12] = struct.pack("<H", 1)
    data[i + 12:i + 16] = struct.pack("<I", 10_000_000)  # directory claims to run past the file end
    p.write_bytes(bytes(data))
    r = estimate_one(p)
    assert r.detail["archive_toc"] == "unreadable" and r.band.expected >= 1


def test_nested_archive_counts_by_size_unpacked(tmp_path):
    inner = tmp_path / "inner.zip"
    _zip(inner, {"a.txt": "x"})
    outer = tmp_path / "outer.zip"
    _zip(outer, {"inner.zip": inner.read_bytes()})
    d = estimate_one(outer).detail
    assert d["archive_nested_archives"] == 1 and d["unpacked_units_est"] >= 1.0


def test_unreadable_archive_still_one_unit(tmp_path):
    p = tmp_path / "broken.zip"
    p.write_bytes(b"not a zip at all")
    r = estimate_one(p)
    assert r.error is None and r.band.expected >= 1
    assert r.detail["archive_toc"] == "unreadable"


def test_rar_has_no_toc_but_counts(tmp_path):
    p = tmp_path / "x.rar"
    p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\0" * 64)
    r = estimate_one(p)
    assert r.band.expected >= 1 and r.detail["archive_members"] == 0 and r.detail["archive_toc"] == "not listable"


def test_unlisted_archive_is_counted_by_size_not_as_one_unit(tmp_path):
    p = tmp_path / "big.rar"
    p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\0" * 20_000_000)
    r = estimate_one(p)
    assert r.band.expected > 100
    g, m = measures_for(r.detail, r.ext, r.size_bytes)
    assert g == "archive" and m["unlisted_files"] == 1 and m["bytes"] == r.size_bytes  # counted by size only
