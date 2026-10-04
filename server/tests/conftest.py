import os
import struct
from pathlib import Path

import pytest


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def make_mp4(path: Path, duration: float = 60.0, width: int = 3840, height: int = 1920,
             mdat_bytes: int = 4096, moov_first: bool = True) -> Path:
    """Write a structurally valid (but undecodable) MP4 with the given duration/size."""
    timescale = 1000
    mvhd = box(b"mvhd", bytes([0, 0, 0, 0]) + struct.pack(">IIII", 0, 0, timescale, int(duration * timescale))
               + bytes(80))
    tkhd = box(b"tkhd", bytes([0, 0, 0, 3]) + bytes(76) + struct.pack(">II", width << 16, height << 16))
    hdlr = box(b"hdlr", bytes(4) + bytes(4) + b"vide" + bytes(12) + b"video\x00")
    trak = box(b"trak", tkhd + box(b"mdia", hdlr))
    moov = box(b"moov", mvhd + trak)
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    mdat = box(b"mdat", bytes(range(256)) * (mdat_bytes // 256))
    path.write_bytes(ftyp + (moov + mdat if moov_first else mdat + moov))
    return path


@pytest.fixture
def content_dir(tmp_path):
    d = tmp_path / "content"
    d.mkdir()
    make_mp4(d / "concert_360_TB.mp4", duration=120.0)
    make_mp4(d / "trailer_flat.mp4", duration=30.0, width=1920, height=1080, mdat_bytes=300_000)
    return d


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])
