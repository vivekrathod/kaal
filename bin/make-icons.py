#!/usr/bin/env python3
"""Draw the web app's PNG icons from the same shapes as kaal/web/icon.svg.

Browsers ask for /favicon.ico, and phones for /apple-touch-icon.png, whatever
the page declares; neither accepts an SVG everywhere. Run this after changing
the icon.
"""
from pathlib import Path

from PIL import Image, ImageDraw

WEB = Path(__file__).resolve().parents[1] / "kaal" / "web"
GREEN, WHITE = (31, 95, 85, 255), (255, 255, 255, 255)
SCALE = 8  # drawn large and shrunk, for smooth edges


def draw(size: int, *, square: bool) -> Image.Image:
    unit = size * SCALE / 64
    at = lambda *numbers: tuple(round(number * unit) for number in numbers)  # noqa: E731
    image = Image.new("RGBA", (size * SCALE, size * SCALE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle(at(0, 0, 64, 64), radius=0 if square else round(14 * unit), fill=GREEN)
    # The receipt: a sheet with a torn lower edge.
    pen.rounded_rectangle(at(18, 14, 46, 30), radius=round(2 * unit), fill=WHITE)
    pen.polygon([at(18, 20), at(46, 20), at(46, 50), at(41, 47), at(36.5, 50), at(32, 47), at(27.5, 50), at(23, 47), at(18, 50)], fill=WHITE)
    width = round(3 * unit)
    for y, end in ((24, 39), (31, 39), (38, 33)):
        pen.line([at(25, y), at(end, y)], fill=GREEN, width=width)
        for x in (25, end):
            pen.ellipse([round(x * unit - width / 2), round(y * unit - width / 2), round(x * unit + width / 2), round(y * unit + width / 2)], fill=GREEN)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    # A phone masks its home-screen icon itself, so that one is a full square.
    for name, size, square in (("icon-32.png", 32, False), ("icon-180.png", 180, True), ("icon-512.png", 512, False)):
        draw(size, square=square).save(WEB / name, optimize=True)
        print(f"wrote kaal/web/{name}")


if __name__ == "__main__":
    main()
