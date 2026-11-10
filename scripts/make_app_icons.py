"""Generate the desktop-shell icon set from one drawing.

The Tauri scaffolding cannot ship placeholder icons, and `npx tauri icon` needs a
1024px source that a build machine would have to keep in the tree. This script
draws the same mark the sidebar uses (an orbital ring around a cyan core) with
Pillow at 8x supersampling, so the committed PNG/ICO/ICNS files are reproducible
from source with no binary blob of unknown origin:

    python scripts/make_app_icons.py

Writes into ``frontend/src-tauri/icons/``:
    32x32.png, 128x128.png, 128x128@2x.png, icon.png, icon.ico, icon.icns

Requires Pillow (``pip install pillow``); it is a build-machine tool, not a
runtime dependency of the assistant.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

TARGET = Path(__file__).resolve().parent.parent / "frontend" / "src-tauri" / "icons"
SCALE = 8  # supersampling factor

NIGHT = (5, 11, 22, 255)
RING = (34, 211, 238, 255)  # electric cyan
CORE = (14, 165, 194, 255)
GLOW = (56, 189, 248, 70)


def draw(size: int) -> Image.Image:
    """Draw the mark at ``size`` px, drawn large and downsampled."""

    canvas = size * SCALE
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw_ctx = ImageDraw.Draw(image)

    centre = canvas / 2
    # Rounded-square plate so the icon reads on light and dark backgrounds.
    radius = canvas * 0.22
    draw_ctx.rounded_rectangle(
        (canvas * 0.04, canvas * 0.04, canvas * 0.96, canvas * 0.96),
        radius=radius,
        fill=NIGHT,
        outline=(44, 71, 102, 255),
        width=max(1, int(canvas * 0.012)),
    )

    # Outer orbital ring (open, four gaps) plus one tilted ellipse.
    ring_r = canvas * 0.36
    box = (centre - ring_r, centre - ring_r, centre + ring_r, centre + ring_r)
    width = max(2, int(canvas * 0.022))
    for start in (8, 98, 188, 278):
        draw_ctx.arc(box, start=start, end=start + 66, fill=RING, width=width)

    ellipse_w, ellipse_h = canvas * 0.44, canvas * 0.17
    draw_ctx.ellipse(
        (
            centre - ellipse_w,
            centre - ellipse_h,
            centre + ellipse_w,
            centre + ellipse_h,
        ),
        outline=(167, 139, 250, 170),
        width=max(1, int(canvas * 0.012)),
    )

    # Soft core glow, then the solid core and its inner highlight.
    glow_r = canvas * 0.20
    for step in range(9, 0, -1):
        factor = step / 9
        alpha = int(GLOW[3] * (1 - factor) + 12)
        draw_ctx.ellipse(
            (
                centre - glow_r * factor,
                centre - glow_r * factor,
                centre + glow_r * factor,
                centre + glow_r * factor,
            ),
            fill=(GLOW[0], GLOW[1], GLOW[2], alpha),
        )
    core_r = canvas * 0.115
    draw_ctx.ellipse(
        (centre - core_r, centre - core_r, centre + core_r, centre + core_r),
        fill=CORE,
    )
    inner_r = core_r * 0.45
    draw_ctx.ellipse(
        (centre - inner_r, centre - inner_r, centre + inner_r, centre + inner_r),
        fill=(224, 251, 255, 255),
    )

    return image.resize((size, size), Image.LANCZOS)


def png(size: int) -> bytes:
    buffer = io.BytesIO()
    draw(size).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def icns(entries: dict[bytes, int]) -> bytes:
    """Minimal ICNS container: PNG chunks only (modern macOS reads these)."""

    chunks = b""
    for kind, size in entries.items():
        data = png(size)
        chunks += kind + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(chunks) + 8) + chunks


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int]] = []

    def write(name: str, data: bytes) -> None:
        path = TARGET / name
        path.write_bytes(data)
        written.append((name, len(data)))

    write("32x32.png", png(32))
    write("128x128.png", png(128))
    write("128x128@2x.png", png(256))
    write("icon.png", png(512))

    ico = io.BytesIO()
    draw(256).save(ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    write("icon.ico", ico.getvalue())

    write(
        "icon.icns",
        icns(
            {
                b"ic07": 128,
                b"ic08": 256,
                b"ic09": 512,
                b"ic13": 256,
                b"ic14": 512,
            }
        ),
    )

    for name, size in written:
        print(f"  {name:14s} {size:>7d} bytes")


if __name__ == "__main__":
    main()
