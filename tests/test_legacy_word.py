"""Legacy .doc piece-table reader, on synthetic WordDocument / table streams."""

import time

import pytest

from poma_estimate.estimators.legacy_word import _drop_field_codes, _piece_text


def _streams(pieces: list[tuple[str, bool]]) -> tuple[bytes, bytes]:
    """Build a minimal FIB + text area and a table stream whose CLX lists `pieces`."""
    wd = bytearray(0x400)
    wd[0:2] = (0xA5EC).to_bytes(2, "little")
    cps, pcds, cp = [0], [], 0
    for text, compressed in pieces:
        off = len(wd)
        data = text.encode("cp1252") if compressed else text.encode("utf-16-le")
        wd += data
        fc = (off * 2) | 0x40000000 if compressed else off
        pcds.append(b"\x00\x00" + fc.to_bytes(4, "little") + b"\x00\x00")
        cp += len(text)
        cps.append(cp)
    plc = b"".join(c.to_bytes(4, "little") for c in cps) + b"".join(pcds)
    clx = b"\x01\x02\x00ab" + b"\x02" + len(plc).to_bytes(4, "little") + plc  # one Prc, then the Pcdt
    table = b"\xff" * 16 + clx
    wd[0x4C:0x50] = cp.to_bytes(4, "little")  # ccpText: all of it is main-document text
    wd[0x1A2:0x1A6] = (16).to_bytes(4, "little")
    wd[0x1A6:0x1AA] = len(clx).to_bytes(4, "little")
    return bytes(wd), table


def test_mixed_pieces_read_once():
    wd, tbl = _streams([("Hello world\r", True), ("Grüße ΑΒΓ\r", False)])
    assert _piece_text(wd, tbl) == "Hello world\nGrüße ΑΒΓ\n"


def test_field_instructions_dropped_results_kept():
    wd, tbl = _streams([("see \x13 HYPERLINK \"x\" \x14the site\x15 now", True)])
    assert _piece_text(wd, tbl) == "see the site now"


def test_not_word97_returns_none_and_encrypted_raises():
    wd, tbl = _streams([("x", True)])
    assert _piece_text(b"\x00" * len(wd), tbl) is None
    encrypted = bytearray(wd)
    encrypted[0x0B] |= 0x01  # flags high byte: fEncrypted (0x0100)
    with pytest.raises(ValueError):
        _piece_text(bytes(encrypted), tbl)


def _cp_offset(tbl: bytes, k: int) -> int:
    return 16 + 5 + 5 + 4 * k  # table padding, the Prc, the Pcdt header, then CP k


def test_inconsistent_piece_tables_rejected():
    wd, tbl = _streams([("Hello world\r", True), ("second piece", False)])
    assert _piece_text(wd, tbl) is not None
    bad = bytearray(tbl)  # decreasing CPs
    bad[_cp_offset(tbl, 1):_cp_offset(tbl, 1) + 4] = (10**6).to_bytes(4, "little")
    assert _piece_text(wd, bytes(bad)) is None
    bad = bytearray(tbl)  # last CP no longer covers the declared main text
    bad[_cp_offset(tbl, 2):_cp_offset(tbl, 2) + 4] = (13).to_bytes(4, "little")
    assert _piece_text(wd, bytes(bad)) is None
    assert _piece_text(wd[:-4], tbl) is None  # the last piece runs past the stream
    big = bytearray(wd)  # declared footnotes the pieces do not cover
    big[0x50:0x54] = (5).to_bytes(4, "little")
    assert _piece_text(bytes(big), tbl) is None
    bad = bytearray(tbl)  # Pcdt length larger than the CLX
    bad[16 + 5 + 1:16 + 5 + 5] = (10**6).to_bytes(4, "little")
    assert _piece_text(wd, bytes(bad)) is None


def test_deeply_nested_fields_are_linear():
    text = "\x13a\x14" * 200_000 + "x" + "\x15" * 200_000
    start = time.monotonic()
    assert _drop_field_codes(text) == "x"
    assert time.monotonic() - start < 2.0
