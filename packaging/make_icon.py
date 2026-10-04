"""Writes packaging/icon.png and packaging/icon.ico (stdlib only; a play triangle on a dark tile)."""
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def render(size: int) -> bytes:
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            u, v = x / size, y / size
            inside_tile = min(u, 1 - u, v, 1 - v) > 0.04
            if not inside_tile:
                px = (0, 0, 0, 0)
            elif 0.33 < u < 0.73 and abs(v - 0.5) < 0.25 * (0.73 - u) / 0.40:
                px = (255, 255, 255, 255)
            else:
                px = (28, 100, 200, 255)
            row += bytes(px)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    png256 = render(256)
    (HERE / "icon.png").write_bytes(png256)
    # ICO with a single embedded 256x256 PNG (supported since Windows Vista)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png256), 22)
    (HERE / "icon.ico").write_bytes(header + entry + png256)


if __name__ == "__main__":
    main()
