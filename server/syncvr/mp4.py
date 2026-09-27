"""Minimal ISO-BMFF (MP4/MOV) reader: duration and video dimensions.

Only walks box headers, so it is fast even for multi-gigabyte files and
does not care whether the ``moov`` box is at the start or the end.
"""

import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Optional, Tuple

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


@dataclass
class Mp4Info:
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    faststart: Optional[bool] = None  # moov before mdat (better for streaming, irrelevant for local files)


def _boxes(f: BinaryIO, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """Yield (type, payload_start, box_end) for each box in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        header_len = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len or pos + size > end:
            return
        yield kind, pos + header_len, pos + size
        pos += size


def _read(f: BinaryIO, pos: int, n: int) -> bytes:
    f.seek(pos)
    data = f.read(n)
    if len(data) < n:
        raise ValueError("truncated box")
    return data


def _parse_mvhd(f: BinaryIO, start: int) -> Optional[float]:
    version = _read(f, start, 1)[0]
    if version == 1:
        timescale, duration = struct.unpack(">IQ", _read(f, start + 4 + 16, 12))
    else:
        timescale, duration = struct.unpack(">II", _read(f, start + 4 + 8, 8))
    if timescale == 0:
        return None
    return duration / timescale


def _parse_trak(f: BinaryIO, start: int, end: int) -> Tuple[Optional[bytes], Optional[Tuple[int, int]]]:
    handler = None
    dims = None
    for kind, pstart, pend in _boxes(f, start, end):
        if kind == b"tkhd" and pend - pstart >= 8:
            w, h = struct.unpack(">II", _read(f, pend - 8, 8))
            dims = (w >> 16, h >> 16)
        elif kind == b"mdia":
            for k2, s2, e2 in _boxes(f, pstart, pend):
                if k2 == b"hdlr" and e2 - s2 >= 12:
                    handler = _read(f, s2 + 8, 4)
    return handler, dims


def read_mp4_info(path) -> Mp4Info:
    info = Mp4Info()
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_end = f.tell()
        seen_mdat = False
        for kind, pstart, pend in _boxes(f, 0, file_end):
            if kind == b"mdat":
                seen_mdat = True
            elif kind == b"moov":
                info.faststart = not seen_mdat
                for k2, s2, e2 in _boxes(f, pstart, pend):
                    if k2 == b"mvhd":
                        info.duration = _parse_mvhd(f, s2)
                    elif k2 == b"trak":
                        handler, dims = _parse_trak(f, s2, e2)
                        if handler == b"vide" and dims and info.width is None:
                            info.width, info.height = dims
                break
    return info
