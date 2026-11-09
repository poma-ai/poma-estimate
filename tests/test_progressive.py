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


def test_projection_scales_sampling_and_floor_parts():
    from poma_estimate.output import stage_line
    from poma_estimate.progressive import Estimate, Z95, project

    est = Estimate(expected=1000.0, low=0, high=0, rel_err=0.0, complete=False, exact=False, measured=100,
                   total=1000, measured_bytes=0, total_bytes=0, stage="measuring", elapsed=10.0,
                   var_sampling=100.0 ** 2, var_floor=50.0 ** 2)
    est.rel_err = Z95 * (100.0 ** 2 + 50.0 ** 2) ** 0.5 / 1000
    project(est, rate=10.0, seconds_left=40.0)  # 400 more files: n 100 -> 500 of 1,000
    assert est.eta_all == 90.0
    k_s = (0.5 / 500) / (0.9 / 100)
    k_f = (0.5 / 0.9) ** 2
    assert abs(est.rel_err_at_budget - Z95 * (100.0 ** 2 * k_s + 50.0 ** 2 * k_f) ** 0.5 / 1000) < 1e-12
    line = stage_line(est)
    assert "10.0% measured (100/1,000 files)" in line and "by the time limit" in line and "all files in ~2 min" in line
    project(est, rate=10.0, seconds_left=1e6)
    assert est.rel_err_at_budget == 0.0



def test_band_of_measured_strata_ignores_size_model_bias(monkeypatch):
    """Once every stratum has samples, a size model that is 10x off must not widen the band."""
    import poma_estimate.progressive as pg
    from poma_estimate.credit_math import CreditBand
    from poma_estimate.scan import FileResult, ScanReport

    ents = [pg.Entry(Path(f"/x/f{i}.txt"), "txt", 4000 + i) for i in range(200)]
    strata = {("txt", 6): ents}
    measured = {("txt", 6): [FileResult(e.path, "txt", e.size, CreditBand(u, u, u), {})
                             for e, u in zip(ents[:20], [3, 4, 5, 6, 4] * 4)]}
    real = pg.rough_units

    def rel(scale):
        monkeypatch.setattr(pg, "rough_units", lambda ext, size: scale * real(ext, size))
        return pg.combine(strata, measured, {}, "t", 0.0, ScanReport()).rel_err

    assert 0 < rel(1.0) == rel(10.0) < 1


def test_process_pool_matches_threads(tmp_path):
    for i in range(30):
        (tmp_path / f"f{i}.txt").write_text("word " * (300 + i * 50))
    _, a = scan_progressive(tmp_path, workers=2)
    _, b = scan_progressive(tmp_path, workers=2, processes=True)
    assert a.complete and b.complete and a.expected == b.expected and a.counts == b.counts


def test_process_pool_survives_a_crashing_worker(tmp_path, monkeypatch):
    """A worker that dies (native crash, killed for memory) costs only its file, not the scan."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "import os, poma_estimate.scan as sc\n"
        "_real = sc.estimate_one\n"
        "def _crash(path, fast=False):\n"
        "    if path.name == 'crash.txt':\n"
        "        os._exit(9)\n"
        "    return _real(path, fast)\n"
        "sc.estimate_one = _crash\n")
    data = tmp_path / "data"
    data.mkdir()
    for i in range(40):
        (data / f"f{i}.txt").write_text("word " * (300 + i))
    (data / "crash.txt").write_text("word " * 300)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(site), os.environ.get("PYTHONPATH", "")]))
    report, est = scan_progressive(data, workers=2, processes=True)
    assert est.complete and est.stopped is None and est.total == 41 and est.unreadable_files == 1
    crashed = [r for r in report.results if r.error]
    assert [r.path.name for r in crashed] == ["crash.txt"]  # only the crashing file, and it still counts by size
    assert est.expected == sum(r.band.expected for r in report.results if not r.error) + progressive.rough_units(
        "txt", crashed[0].size_bytes)
