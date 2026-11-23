import struct
from poma_estimate.estimators.media import estimate_media


def test_wav_header_duration(tmp_path):
    byte_rate = 176_400  # 44.1 kHz stereo 16-bit
    data = b"\0" * (byte_rate * 90)  # 90 s
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 2, 44100, byte_rate, 4, 16)
    p = tmp_path / "a.wav"
    p.write_bytes(hdr + b"data" + struct.pack("<I", len(data)) + data)
    band, d = estimate_media(p, "wav", p.stat().st_size)
    assert d["method"] == "header" and band.expected == 2


def test_mp4_mvhd_duration(tmp_path):
    mvhd = struct.pack(">I4sB3x", 108, b"mvhd", 0) + struct.pack(">IIII", 0, 0, 1000, 125_000) + b"\0" * 80
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0"
    p = tmp_path / "v.mp4"
    p.write_bytes(ftyp + moov)
    band, d = estimate_media(p, "mp4", p.stat().st_size)
    assert d["method"] == "header" and band.expected == 3  # 125 s


def test_size_fallback_is_banded(tmp_path):
    p = tmp_path / "x.mkv"
    p.write_bytes(b"\0" * 200_000_000)
    band, d = estimate_media(p, "mkv", p.stat().st_size)
    assert d["method"] == "size" and band.low < band.expected < band.high


def test_wav_ignores_container_bytes(tmp_path):
    byte_rate = 16_000
    data = b"\0" * (byte_rate * 60)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 8000, byte_rate, 2, 16)
    p = tmp_path / "a.wav"
    p.write_bytes(hdr + b"data" + struct.pack("<I", len(data)) + data)
    band, d = estimate_media(p, "wav", p.stat().st_size)
    assert band.expected == 1 and d["method"] == "header"


def test_truncated_mp4_falls_back_without_crashing(tmp_path):
    p = tmp_path / "t.mp4"
    p.write_bytes(struct.pack(">I4sI4s", 16, b"moov", 8, b"mvhd"))
    band, d = estimate_media(p, "mp4", p.stat().st_size)
    assert d["method"] == "size" and band.expected >= 1


def test_vbr_mp3_uses_xing_frame_count(tmp_path):
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\0" * 32  # MPEG1 L3, 128k, 44.1k
    xing = b"Xing" + struct.pack(">II", 1, 10_000)  # 10,000 frames
    p = tmp_path / "v.mp3"
    p.write_bytes(frame + xing + b"\0" * 4000)
    band, d = estimate_media(p, "mp3", p.stat().st_size)
    assert d["method"] == "header" and band.expected == 5  # 10,000*1152/44100 = 261 s = 4.35 min
