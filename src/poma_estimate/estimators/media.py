"""Audio + video: one minute of media counts as one unit.

Duration is read from container headers where that is cheap and exact
(MP4/MOV/M4A family `mvhd`, WAV, MP3 first frame bitrate) and otherwise
triangulated from file size with a typical bytes-per-minute figure per
format. Heuristic results carry a wide low/high band.
"""

import math
import struct
from pathlib import Path

from poma_estimate.credit_math import CreditBand

AUDIO_EXTS = {
    "mp3", "wav", "m4a", "aac", "flac", "ogg", "oga", "opus", "wma",
    "aiff", "aif", "amr",
}
VIDEO_EXTS = {
    "mp4", "mov", "avi", "mkv", "webm", "wmv", "flv", "m4v", "mpg",
    "mpeg", "3gp", "ts", "vob",
}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

_MB = 1_000_000
# (low, typical, high) bytes per minute; low/high bound the plausible spread.
_BYTES_PER_MIN = {
    "aac": (0.4 * _MB, 1 * _MB, 2 * _MB), "m4a": (0.4 * _MB, 1 * _MB, 2.5 * _MB),
    "ogg": (0.3 * _MB, 1 * _MB, 2.5 * _MB), "oga": (0.3 * _MB, 1 * _MB, 2.5 * _MB),
    "opus": (0.2 * _MB, 0.6 * _MB, 1.5 * _MB), "wma": (0.4 * _MB, 1 * _MB, 2 * _MB),
    "mp3": (0.5 * _MB, 1 * _MB, 2.5 * _MB), "flac": (3 * _MB, 6 * _MB, 12 * _MB),
    "wav": (5 * _MB, 10.6 * _MB, 21 * _MB), "aiff": (5 * _MB, 10.6 * _MB, 21 * _MB),
    "aif": (5 * _MB, 10.6 * _MB, 21 * _MB), "amr": (0.05 * _MB, 0.1 * _MB, 0.2 * _MB),
}
_VIDEO_BPM = (4 * _MB, 20 * _MB, 80 * _MB)


def _units(minutes: float) -> int:
    return max(1, math.ceil(minutes - 1e-6))


def _mp4_minutes(f, size: int) -> float | None:
    """Walk top-level atoms (seeks only) to moov/mvhd. None if malformed."""
    pos = 0
    while pos + 8 <= size:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return None
        n, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if n == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return None
            n = struct.unpack(">Q", ext)[0]
            hlen = 16
        elif n == 0:
            n = size - pos
        if n < hlen:
            return None
        if typ == b"moov":
            end, p = min(pos + n, size), pos + hlen
            while p + 8 <= end:
                f.seek(p)
                head = f.read(8)
                if len(head) < 8:
                    return None
                m, t = struct.unpack(">I4s", head)
                chl = 8
                if m == 1:
                    big = f.read(8)
                    if len(big) < 8:
                        return None
                    m, chl = struct.unpack(">Q", big)[0], 16
                if m < chl or p + m > end:
                    return None  # child escapes its parent
                if t == b"mvhd":
                    body = f.read(32)
                    if len(body) < 32 or p + chl + 32 > min(end, p + m):
                        return None
                    if body[0] == 1:
                        ts, dur = struct.unpack(">IQ", body[20:32])
                    elif body[0] == 0:
                        ts, dur = struct.unpack(">II", body[12:20])
                    else:
                        return None
                    return dur / ts / 60 if ts else None
                p += m
            return None
        pos += n
    return None


def _wav_minutes(f, size: int) -> float | None:
    """Walk RIFF chunks: payload bytes of `data` / byte rate of `fmt `
    (RF64: sizes from `ds64`). Chunk sizes are bounded by the file."""
    head = f.read(12)
    if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
        return None
    byte_rate = data_len = ds64_data = None
    if head[:4] == b"RIFF":
        size = min(size, 8 + struct.unpack("<I", head[4:8])[0])  # declared container end
    pos = 12
    while pos + 8 <= size and (byte_rate is None or data_len is None):
        f.seek(pos)
        h = f.read(8)
        if len(h) < 8:
            break
        cid, n = struct.unpack("<4sI", h)
        room = size - pos - 8
        if cid == b"fmt ":
            body = f.read(16)
            if n < 16 or len(body) < 16:
                return None
            byte_rate = struct.unpack("<I", body[8:12])[0]
        elif cid == b"ds64":
            body = f.read(16)
            if len(body) == 16:
                ds64_data = struct.unpack("<8xQ", body)[0]
        elif cid == b"data":
            if n == 0xFFFFFFFF and ds64_data is not None:
                data_len = min(ds64_data, room)
            elif n == 0xFFFFFFFF:
                data_len = room
            else:
                data_len = min(n, room)
        pos += 8 + n + (n & 1)
    if not byte_rate or data_len is None:
        return None
    return data_len / byte_rate / 60


_MP3_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
_MP3_RATES = [44100, 48000, 32000]


def _mp3_minutes(f, size: int) -> tuple[float, bool] | None:
    """(minutes, exact). Exact when a Xing/Info/VBRI frame count exists;
    otherwise the first frame's bitrate is assumed constant (approximate)."""
    head = f.read(10)
    skip = 0
    if head[:3] == b"ID3" and len(head) == 10:
        skip = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14
                     | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
    f.seek(skip)
    buf = f.read(4096)
    for i in range(len(buf) - 4):
        if buf[i] == 0xFF and (buf[i + 1] & 0xFE) == 0xFA:  # MPEG1 Layer III
            br, sr = buf[i + 2] >> 4, (buf[i + 2] >> 2) & 3
            if not (0 < br < 15 and sr < 3):
                continue
            rate = _MP3_RATES[sr]
            mono = (buf[i + 3] >> 6) == 3
            side = 4 + (17 if mono else 32)  # tag sits right after the side info
            tag = buf[i + side:i + side + 4]
            if tag in (b"Xing", b"Info") and len(buf) >= i + side + 12:
                if buf[i + side + 7] & 1:
                    frames = struct.unpack(">I", buf[i + side + 8:i + side + 12])[0]
                    if frames:
                        return frames * 1152 / rate / 60, True
            if buf[i + 4 + 32:i + 4 + 36] == b"VBRI" and len(buf) >= i + 4 + 32 + 18:
                frames = struct.unpack(">I", buf[i + 36 + 14:i + 36 + 18])[0]
                if frames:
                    return frames * 1152 / rate / 60, True
            return (size - skip) * 8 / (_MP3_BITRATES[br] * 1000) / 60, False
    return None


def estimate_media(path: Path, ext: str, size: int, **_) -> tuple[CreditBand, dict]:
    kind = "audio" if ext in AUDIO_EXTS else "video"
    minutes = None
    spread = (1.0, 1.0)  # (low, high) multipliers on the header-derived duration
    try:
        with open(path, "rb") as f:
            if ext in ("mp4", "mov", "m4a", "m4v", "3gp"):
                minutes = _mp4_minutes(f, size)
            elif ext == "wav":
                minutes = _wav_minutes(f, size)
            elif ext == "mp3":
                r = _mp3_minutes(f, size)
                if r:
                    minutes = r[0]
                    spread = (1.0, 1.0) if r[1] else (0.75, 1.3)
    except (OSError, struct.error, IndexError, ValueError):
        minutes = None
    if minutes is not None and minutes > 0:
        band = CreditBand(_units(minutes * spread[0]), _units(minutes), _units(minutes * spread[1]))
        return band, {"kind": kind, "minutes": round(minutes, 1),
                      "method": "header" if spread == (1.0, 1.0) else "size"}
    lo, mid, hi = _BYTES_PER_MIN.get(ext, _VIDEO_BPM)
    band = CreditBand(_units(size / hi), _units(size / mid), _units(size / lo))
    return band, {"kind": kind, "minutes": round(size / mid, 1), "method": "size"}
