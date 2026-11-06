"""Folder walker + dispatch."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from poma_estimate.credit_math import CreditBand
from poma_estimate.magic import detect_real_ext
from poma_estimate.estimators import (
    EPUB_EXTS,
    HTML_EXTS,
    IMAGE_EXTS,
    LEGACY_OFFICE_EXTS,
    LEGACY_WORD_EXTS,
    OFFICE_EXTS,
    OPENDOC_EXTS,
    RTF_EXTS,
    TEXT_EXTS,
    estimate_epub,
    estimate_html,
    estimate_image,
    estimate_legacy_office,
    estimate_legacy_word,
    estimate_office,
    estimate_opendoc,
    estimate_pdf,
    estimate_rtf,
    estimate_text,
)
from poma_estimate.estimators.archive import ARCHIVE_EXTS, estimate_archive
from poma_estimate.estimators.media import MEDIA_EXTS, estimate_media


@dataclass
class FileResult:
    path: Path
    ext: str
    size_bytes: int
    band: CreditBand
    detail: dict
    error: str | None = None


@dataclass
class ScanReport:
    """Summary of a folder scan beyond the per-file results."""

    results: list[FileResult] = field(default_factory=list)
    icloud_skipped: list[Path] = field(default_factory=list)
    unsupported: int = 0
    unreadable_dirs: int = 0
    walk_incomplete: bool = False


_SKIPPED_EXTS = {"ds_store"}

# macOS marks iCloud Drive placeholder files (data not on disk) with
# the BSD UF_DATALESS flag. Opening such a file triggers a network
# download — which on a 2k-file Downloads folder means hammering iCloud
# for hundreds of MB. We skip these by default and report them so the
# customer can choose whether to materialize.
_UF_DATALESS = 0x40000000


def _is_icloud_placeholder(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    flags = getattr(st, "st_flags", 0)
    return bool(flags & _UF_DATALESS)


def _classify(ext: str) -> str | None:
    if ext == "pdf":
        return "pdf"
    if ext in MEDIA_EXTS:
        return "media"
    if ext in OFFICE_EXTS:
        return "office"
    if ext in LEGACY_OFFICE_EXTS:
        return "legacy_office"
    if ext in LEGACY_WORD_EXTS:
        return "legacy_word"
    if ext in OPENDOC_EXTS:
        return "opendoc"
    if ext in RTF_EXTS:
        return "rtf"
    if ext in EPUB_EXTS:
        return "epub"
    if ext in HTML_EXTS:
        return "html"
    if ext in TEXT_EXTS:
        return "text"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    return None


def iter_files(root: Path, recursive: bool = True) -> Iterator[Path]:
    """Yield regular files; skip hidden entries and never follow symlinks."""
    if root.is_file():
        yield root
        return
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if recursive:
                            stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        yield Path(e.path)
                except OSError:
                    continue


def estimate_one(path: Path, fast: bool = False) -> FileResult:
    claimed_ext = path.suffix.lstrip(".").lower()
    size = path.stat().st_size

    # Magic-byte sniff in case the extension lies. detect_real_ext
    # returns None when there's no mismatch, so the hot path is just
    # one tiny read.
    real_ext = detect_real_ext(path, claimed_ext)
    if real_ext is not None and real_ext not in ("zip", "ole"):
        # zip/ole are ambiguous — could be any OOXML/ODF or
        # doc/xls/ppt. Trust the user's extension in those cases; the
        # estimator's own zip+xml fallback handles content-type quirks.
        ext = real_ext
    else:
        ext = claimed_ext

    kind = _classify(ext)
    if kind is None:
        # Unsupported extension: skip with zero band. Caller filters.
        from poma_estimate.credit_math import CreditBand as _CB
        return FileResult(
            path=path, ext=ext, size_bytes=size,
            band=_CB(0, 0, 0), detail={"kind": "skipped"},
            error="unsupported extension",
        )
    try:
        if kind == "archive":
            band, detail = estimate_archive(path, ext)
        elif kind == "media":
            band, detail = estimate_media(path, ext, size)
        elif kind == "pdf":
            band, detail = estimate_pdf(path, fast=fast)
        elif kind == "office":
            band, detail = estimate_office(path, fast=fast)
        elif kind == "legacy_office":
            band, detail = estimate_legacy_office(path)
        elif kind == "legacy_word":
            band, detail = estimate_legacy_word(path)
        elif kind == "opendoc":
            band, detail = estimate_opendoc(path)
        elif kind == "rtf":
            band, detail = estimate_rtf(path)
        elif kind == "epub":
            band, detail = estimate_epub(path)
        elif kind == "html":
            band, detail = estimate_html(path)
        elif kind == "text":
            band, detail = estimate_text(path)
        else:
            band, detail = estimate_image(path)
        if kind == "image":  # the decoded format decides the count, whatever the file is called
            decoded = {"jpeg": "jpg", "mpo": "jpg", "tiff": "tiff", "png": "png", "gif": "gif", "webp": "webp",
                       "bmp": "bmp", "heif": "heic", "ico": "ico"}.get(detail.get("image_format") or "")
            if decoded and not (decoded == ext or {decoded, ext} <= {"tif", "tiff"} or {decoded, ext} <= {"jpg", "jpeg"}):
                ext = decoded
        if real_ext is not None and real_ext != claimed_ext:
            detail["claimed_ext"] = claimed_ext
            detail["real_ext"] = real_ext
        return FileResult(path=path, ext=ext, size_bytes=size, band=band, detail=detail)
    except Exception as exc:
        from poma_estimate.credit_math import CreditBand as _CB
        return FileResult(
            path=path, ext=ext, size_bytes=size,
            band=_CB(0, 0, 0), detail={"kind": kind, "error": str(exc)},
            error=f"{exc.__class__.__name__}: {exc}",
        )


def scan_path(
    root: Path,
    recursive: bool = True,
    include_icloud_placeholders: bool = False,
    fast: bool = False,
    workers: int = 1,
    progress=None,
) -> ScanReport:
    """Walk root, dispatch each file to its estimator.

    workers > 1 uses a ThreadPoolExecutor — most per-file work is I/O
    (disk reads, pdftotext subprocess) where the GIL is released, so
    threads scale well even though the CPU steps are GIL-held. For a
    corpus of millions of files, set workers to cpu_count() or higher.
    """
    report = ScanReport()

    def candidates() -> Iterator[Path]:
        for p in iter_files(root, recursive):
            if _classify(p.suffix.lstrip(".").lower()) is None:
                report.unsupported += 1
                continue
            if not include_icloud_placeholders and _is_icloud_placeholder(p):
                report.icloud_skipped.append(p)
                continue
            yield p

    def tick() -> None:
        if progress and len(report.results) % 500 == 0:
            progress(len(report.results))

    if workers <= 1:
        for p in candidates():
            report.results.append(estimate_one(p, fast=fast))
            tick()
    else:
        # Bounded in-flight window keeps memory flat on huge trees.
        from concurrent.futures import FIRST_COMPLETED, wait
        pending: set = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for p in candidates():
                pending.add(pool.submit(estimate_one, p, fast))
                if len(pending) >= workers * 8:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    report.results.extend(f.result() for f in done)
                    tick()
            for f in as_completed(pending):
                report.results.append(f.result())
        report.results.sort(key=lambda r: str(r.path))
    return report
