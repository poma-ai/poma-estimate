"""Archives (.zip, .tar, ...): read a zip's table of contents only, never extract.

An archive counts as what is inside it: the files have to be processed once unpacked,
so the count must see them (the platform does not unpack archives, so unpack
them before upload). Members are estimated from the listing
by size, the same guess the scanner uses before it measures a file; a nested archive,
or an archive that cannot be listed, is estimated from its own size as if it held
PDFs. Every archive estimate is approximate.

Only zip archives are listed, by walking the central directory with a small parser
of our own: it reads at most MAX_DIRECTORY_BYTES from the position the
end-of-central-directory record gives and stops after MAX_MEMBERS entries, whatever
the headers claim, so no library is asked to allocate on an archive's say-so. ZIP64
archives, tar files (compressed or not), .rar and .7z are not listed.
"""

from __future__ import annotations

import struct
from pathlib import Path

from poma_estimate.credit_math import CreditBand

ARCHIVE_EXTS = frozenset({"zip", "tar", "tgz", "tbz2", "txz", "gz", "bz2", "xz", "7z", "rar"})
MAX_MEMBERS = 100_000
MAX_DIRECTORY_BYTES = 32 * 1024 * 1024

# listing states (archive_toc)
LISTED, TOO_LARGE, UNREADABLE, NOT_LISTABLE, TRUNCATED = "listed", "too large to list", "unreadable", "not listable", "truncated"

_EOCD = b"PK\x05\x06"
_CDH = b"PK\x01\x02"


def _zip_members(path: Path) -> tuple[list[tuple[str, int]], str, int | None]:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 65_557))  # EOCD (22 bytes) + max comment (65,535)
        tail = fh.read()
        i = tail.rfind(_EOCD)
        if i < 0 or len(tail) - i < 22:
            return [], UNREADABLE, None
        _, _, _, entries, cd_size, cd_offset, _ = struct.unpack("<HHHHIIH", tail[i + 4:i + 22])
        if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
            return [], TOO_LARGE, None  # ZIP64: real values live elsewhere; not listed
        if entries > MAX_MEMBERS or cd_size > MAX_DIRECTORY_BYTES:
            return [], TOO_LARGE, entries
        if cd_offset + cd_size > size:
            return [], UNREADABLE, entries
        fh.seek(cd_offset)
        cd = fh.read(cd_size)  # bounded by MAX_DIRECTORY_BYTES above
    out: list[tuple[str, int]] = []
    pos = records = 0
    while pos + 46 <= len(cd):
        if cd[pos:pos + 4] != _CDH:
            return out, (TRUNCATED if out else UNREADABLE), entries
        usize, nlen, xlen, clen = struct.unpack("<I HHH", cd[pos + 24:pos + 34])
        name = cd[pos + 46:pos + 46 + nlen].decode("utf-8", errors="replace")
        if pos + 46 + nlen > len(cd):
            return out, (TRUNCATED if out else UNREADABLE), entries
        records += 1
        if not name.endswith("/"):
            out.append((name, usize))
        pos += 46 + nlen + xlen + clen
        if len(out) >= MAX_MEMBERS:
            return out, TRUNCATED, entries
    if records != entries or pos != len(cd):  # directory shorter or longer than declared
        return out, (TRUNCATED if out else UNREADABLE), entries
    return out, LISTED, entries


def _members(path: Path, ext: str) -> tuple[list[tuple[str, int]], str, int | None]:
    """(name, uncompressed size as declared) per member, listing state, declared entry count."""
    if ext == "zip":
        return _zip_members(path)
    return [], NOT_LISTABLE, None  # tar (any compression), rar, 7z, single-file .gz/.bz2/.xz


def estimate_archive(path: Path, ext: str) -> tuple[CreditBand, dict]:
    from poma_estimate.progressive import rough_units
    from poma_estimate.scan import _classify

    detail: dict = {"kind": "archive", "approx": True, "tokens_expected": 0}
    try:
        members, state, declared = _members(path, ext)
    except Exception as exc:  # any decoder/format error: fall back to the size guess
        members, state, declared = [], UNREADABLE, None
        detail["archive_error"] = exc.__class__.__name__
    units = 0.0
    supported = nested = 0
    supported_bytes = 0
    for name, size in members:
        base = name.rsplit("/", 1)[-1]
        if any(part.startswith(".") for part in name.split("/") if part) or "__MACOSX/" in name:
            continue
        mext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        if mext in ARCHIVE_EXTS:  # not opened: estimated from its declared size, as PDFs
            nested += 1
            units += rough_units("pdf", size)
            supported_bytes += size
            continue
        kind = _classify(mext)
        if kind is None:
            continue
        supported += 1
        supported_bytes += size
        units += rough_units(mext, size)
    if state != LISTED:  # unreadable, too large, not listable, truncated: never below the size guess
        units = max(units, rough_units("pdf", path.stat().st_size))
    detail.update(
        archive_toc=state,
        archive_declared_entries=declared,
        archive_members=len(members),
        archive_member_bytes=sum(s for _, s in members),
        archive_supported_members=supported + nested,
        archive_supported_bytes=supported_bytes,
        archive_nested_archives=nested,
        contents_from="listing" if state == LISTED else "size guess",
        unpacked_units_est=round(units, 1),
        pages=max(1, round(units)),
    )
    exp = max(1, round(units))
    return CreditBand(max(1, round(units * 0.5)), exp, max(1, round(units * 2))), detail
