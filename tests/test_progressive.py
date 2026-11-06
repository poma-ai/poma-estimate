import os
from pathlib import Path

import pytest

from poma_estimate import progressive
from poma_estimate.progressive import scan_progressive


def _mk(tmp_path: Path, n: int):
    for i in range(n):
        (tmp_path / f"f{i}.txt").write_text("word " * (500 + i * 10))
    (tmp_path / ".hidden.txt").write_text("x" * 100_000)
    try:
        (tmp_path / "link.txt").symlink_to(tmp_path / "f0.txt")
    except (OSError, NotImplementedError):  # Windows without symlink privilege
        pass


def test_small_folder_is_complete_and_matches_sum(tmp_path):
    _mk(tmp_path, 12)
    report, est = scan_progressive(tmp_path, workers=2)
    assert est.complete and est.total == 12 and est.measured == 12  # no hidden, no symlink
    assert est.expected == sum(r.band.expected for r in report.results)
    assert est.exact


def test_empty_folder(tmp_path):
    _, est = scan_progressive(tmp_path)
    assert est.expected == 0 and est.total == 0


def test_failed_file_is_not_duplicated(tmp_path, monkeypatch):
    _mk(tmp_path, 9)
    real = progressive.estimate_one
    victim = str(tmp_path / "f3.txt")

    def flaky(path, fast=False):
        if str(path) == victim:
            raise FileNotFoundError(path)
        return real(path, fast)

    monkeypatch.setattr(progressive, "estimate_one", flaky)
    report, est = scan_progressive(tmp_path, workers=2)
    paths = [str(r.path) for r in report.results]
    assert len(paths) == len(set(paths)) == 8 and est.complete


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="POSIX permissions only; root ignores them")
def test_unreadable_folder_marks_incomplete(tmp_path):
    _mk(tmp_path, 3)
    sub = tmp_path / "locked"
    sub.mkdir()
    (sub / "a.txt").write_text("secret " * 100)
    sub.chmod(0)
    try:
        report, est = scan_progressive(tmp_path)
    finally:
        sub.chmod(0o755)
    assert report.unreadable_dirs == 1 and not est.complete and not est.exact


def test_single_unsupported_file_is_ignored(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"abc")
    report, est = scan_progressive(p)
    assert est.total == 0 and report.unsupported == 1


def test_sampling_stage_extrapolates(tmp_path, monkeypatch):
    monkeypatch.setattr(progressive, 'EMIT_INTERVAL', 0)
    _mk(tmp_path, 400)
    stages = []
    report, est = scan_progressive(tmp_path, workers=4, on_stage=stages.append, budget=600)
    assert est.complete  # no early stop: runs to the end
    exact = est.expected
    first_sample = next(s for s in stages if 0.05 * s.total <= s.measured < s.total)
    assert abs(first_sample.expected - exact) / exact < 0.15


def test_media_is_always_exhaustive(tmp_path):
    import struct
    for i in range(30):
        data = b"\0" * (176_400 * (60 if i != 7 else 3600))
        hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
            "<IHHIIHH", 16, 1, 2, 44100, 176_400, 4, 16)
        (tmp_path / f"a{i}.wav").write_bytes(hdr + b"data" + struct.pack("<I", len(data)) + data)
    stages = []
    _, est = scan_progressive(tmp_path, on_stage=stages.append)
    assert est.expected == 29 + 60  # the 1-hour outlier is never missed
    assert all(s.expected >= 60 + 29 for s in stages if s.measured)


def test_corrupt_file_is_never_exact(tmp_path):
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    (tmp_path / "ok.txt").write_text("hello " * 50)
    report, est = scan_progressive(tmp_path)
    assert est.complete and not est.exact and est.unreadable_files == 1


def test_identical_samples_still_carry_uncertainty(tmp_path):
    for i in range(100):
        (tmp_path / f"f{i}.txt").write_text("same words here " * 100)
    stages = []
    import poma_estimate.progressive as pg
    pg.EMIT_INTERVAL = 0
    try:
        scan_progressive(tmp_path, on_stage=stages.append)
    finally:
        pg.EMIT_INTERVAL = 1.5
    first = next(s for s in stages if 0 < s.measured < s.total)
    assert first.rel_err > 0.05
