"""Magic-byte detection tests.

We don't ship binary fixtures — fabricate minimal valid-looking heads
inline so the tests stay self-contained.
"""

import os
import tempfile
from pathlib import Path

from poma_estimate.magic import detect_real_ext


def _write(content: bytes, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=f".{suffix}")
    os.close(fd)  # Windows cannot delete a file with an open descriptor
    p = Path(name)
    p.write_bytes(content)
    return p


def test_gif_with_html_extension_detected():
    p = _write(b"GIF89a\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00", "html")
    try:
        assert detect_real_ext(p, "html") == "gif"
    finally:
        p.unlink()


def test_pdf_correctly_named_is_passthrough():
    p = _write(b"%PDF-1.7\n%\xc7\xec\x8f\xa2\n", "pdf")
    try:
        assert detect_real_ext(p, "pdf") is None
    finally:
        p.unlink()


def test_actual_html_with_html_extension():
    p = _write(b"<!doctype html><html><body>hi</body></html>", "html")
    try:
        assert detect_real_ext(p, "html") is None
    finally:
        p.unlink()


def test_jpeg_with_png_extension():
    p = _write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00", "png")
    try:
        assert detect_real_ext(p, "png") is None  # both in image family
    finally:
        p.unlink()


def test_zip_returns_zip_hint_not_correction():
    # OOXML can't be disambiguated from header alone — return "zip"
    # and let the caller decide. scan.py treats this as a passthrough.
    p = _write(b"PK\x03\x04\x14\x00\x00\x00\x08\x00", "docx")
    try:
        assert detect_real_ext(p, "docx") is None  # zip family includes docx
    finally:
        p.unlink()


def test_empty_file_returns_none():
    p = _write(b"", "pdf")
    try:
        assert detect_real_ext(p, "pdf") is None
    finally:
        p.unlink()
