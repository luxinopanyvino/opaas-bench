"""Derives the meerkat grid from the original PNG.

The mascot is drawn outside this repository. So that the terminal sprite is
*that* image and not a version of mine redrawn by eye, the grid is generated
here programmatically. `tests/test_sprite_matches_png.py` repeats the read and
fails if anyone touches up the generated module by hand.

    python scripts/sprite_from_png.py docs/img/mascot.png

The PNG is not a clean pixel export: it comes rescaled, with smoothed edges and
±2 per-channel noise. So there are two steps to guess, and both are guessed by
measuring, not by assuming:

* **The cell size** is written by hand because measuring it automatically
  fails: the rescaling noise produces short runs that any detector confuses
  with the chunky pixel. It was measured on the smallest feature of the
  drawing, the eye highlight, which takes 17×18 px; with the 691×1045 crop that
  gives 40×60 cells of 17.3 px, square. Estimating by period gave 31 px — most
  features span two cells — and would have eaten the highlight, which is a
  single one.
* **The color of each cell** is the median of the central 50%, away from the
  smoothed edges, and is then snapped to the nearest palette representative.

It requires Pillow and numpy, which are not dependencies of the tool:

    pip install pillow numpy
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

#: Letters by luminance, from darkest to lightest. They are assigned this way so
#: two runs produce the same module.
LETTERS = "KEDMTLBWSNRAGFIJPQUVXYZ"

TRANSPARENT = "."

#: Two colors closer than this are the same color with compression noise.
SAME_COLOUR = 30

#: Meerkat grid, measured on the eye highlight. See the note above.
COLUMNS, ROWS = 40, 60


def _drawing(image: Image.Image) -> np.ndarray:
    """Crops to the drawing. The background is alpha zero, not a color to guess."""
    data = np.asarray(image.convert("RGBA"), dtype=float)
    rows, columns = np.where(data[..., 3] > 128)
    if not len(rows):
        raise SystemExit("the PNG is entirely transparent, there is no drawing to read.")
    return data[rows.min() : rows.max() + 1, columns.min() : columns.max() + 1]


def _representatives(colours: Counter) -> list[tuple[int, int, int]]:
    """Merges the colors that differ only by noise."""
    found: list[list] = []
    for colour, count in colours.most_common():
        for entry in found:
            if sum((a - b) ** 2 for a, b in zip(entry[0], colour, strict=True)) <= SAME_COLOUR**2:
                entry[1] += count
                break
        else:
            found.append([colour, count])
    return [colour for colour, _ in found]


def extract(
    path: Path, columns: int = COLUMNS, rows: int = ROWS
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Reads the PNG and returns (palette, rows). It touches up not a single pixel."""
    drawing = _drawing(Image.open(path))
    height, width, _ = drawing.shape


    cells: list[list[tuple[int, int, int] | None]] = []
    for row in range(rows):
        top, bottom = int(row * height / rows), int((row + 1) * height / rows)
        inner_top = top + (bottom - top) // 4
        inner_bottom = max(bottom - (bottom - top) // 4, inner_top + 1)
        line: list[tuple[int, int, int] | None] = []
        for column in range(columns):
            left, right = int(column * width / columns), int((column + 1) * width / columns)
            inner_left = left + (right - left) // 4
            inner_right = max(right - (right - left) // 4, inner_left + 1)
            middle = np.median(
                drawing[inner_top:inner_bottom, inner_left:inner_right].reshape(-1, 4), axis=0
            )
            line.append(None if middle[3] <= 128 else tuple(int(value) for value in middle[:3]))
        cells.append(line)

    # The renderer uses half blocks: it needs an even number of rows.
    if len(cells) % 2:
        cells.append([None] * columns)

    seen = Counter(colour for line in cells for colour in line if colour)
    palette_colours = _representatives(seen)
    if len(palette_colours) > len(LETTERS):
        raise SystemExit(
            f"{len(palette_colours)} colors and only {len(LETTERS)} letters. "
            "Raise SAME_COLOUR or lengthen LETTERS."
        )

    def nearest(colour: tuple[int, int, int]) -> tuple[int, int, int]:
        return min(
            palette_colours,
            key=lambda rep: sum((a - b) ** 2 for a, b in zip(rep, colour, strict=True)),
        )

    ordered = sorted(
        palette_colours, key=lambda rgb: 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    )
    letters = {colour: LETTERS[index] for index, colour in enumerate(ordered)}
    palette = {letters[colour]: "#{:02x}{:02x}{:02x}".format(*colour) for colour in ordered}
    grid = tuple(
        "".join(TRANSPARENT if colour is None else letters[nearest(colour)] for colour in line)
        for line in cells
    )
    return palette, grid


def render_module(path: Path, palette: dict[str, str], rows: tuple[str, ...]) -> str:
    counted = Counter(pixel for row in rows for pixel in row)
    entries = "\n".join(
        f'    "{letter}": "{value}",  # {counted[letter]} cells'
        for letter, value in palette.items()
    )
    grid = "\n".join(f'    "{row}",' for row in rows)
    return (
        '"""Meerkat grid. Generated, do not edit by hand.\n\n'
        f"Output of `python scripts/sprite_from_png.py {path.as_posix()}`. The mascot\n"
        "is read from the original PNG without retouching it, and a test reads it\n"
        "again to check that what is here is still that image.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "#: Colors exactly as they come out of the PNG, from darkest to lightest.\n"
        f"PALETTE: dict[str, str] = {{\n{entries}\n}}\n\n"
        f"#: {len(rows[0])} columns × {len(rows)} rows.\n"
        f"MASCOT: tuple[str, ...] = (\n{grid}\n)\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generates the sprite grid from the PNG.")
    parser.add_argument("png", type=Path, help="original PNG of the mascot")
    parser.add_argument("--columns", type=int, default=COLUMNS, help="width in cells")
    parser.add_argument("--rows", type=int, default=ROWS, help="height in cells")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("src/opaas/cli/_sprite_data.py"),
        help="data module to write",
    )
    args = parser.parse_args()

    palette, rows = extract(args.png, args.columns, args.rows)
    args.out.write_text(render_module(args.png, palette, rows), encoding="utf-8", newline="")
    print(f"{args.out}: {len(rows[0])}×{len(rows)} cells, {len(palette)} colors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
