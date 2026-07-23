"""Build a multi-size Windows icon from Ogent PNG assets using only stdlib."""

from __future__ import annotations

import struct
from pathlib import Path


SIZES = (16, 24, 32, 48, 64, 128, 256)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ASSETS_DIR = Path(__file__).resolve().parent
PNG_DIR = ASSETS_DIR / "png"
OUTPUT_PATH = ASSETS_DIR / "ogent.ico"


def read_png(size: int) -> bytes:
    path = PNG_DIR / f"ogent-{size}.png"
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a valid PNG")

    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (size, size):
        raise ValueError(
            f"{path} is {width}x{height}; expected {size}x{size}"
        )
    return data


def build_ico() -> None:
    images = [(size, read_png(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + (16 * len(images))
    entries: list[bytes] = []

    for size, data in images:
        dimension = 0 if size == 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(data),
                offset,
            )
        )
        offset += len(data)

    payload = header + b"".join(entries) + b"".join(
        data for _, data in images
    )
    temp_path = OUTPUT_PATH.with_suffix(".ico.tmp")
    temp_path.write_bytes(payload)
    temp_path.replace(OUTPUT_PATH)
    print(
        f"Wrote {OUTPUT_PATH} with {len(images)} PNG images "
        f"({', '.join(f'{size}px' for size, _ in images)})"
    )


if __name__ == "__main__":
    build_ico()
