"""Pixel typeface for the CLI title.

Five by seven, which is the minimum for a letter to stay legible. It is drawn
with the same half blocks as the sprite, so the title and the mascot share the
same grid and the whole thing reads as a single piece.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

GLYPH_WIDTH, GLYPH_HEIGHT, GLYPH_SPACING = 5, 7, 1

_GLYPHS: dict[str, tuple[str, ...]] = {
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "N": ("#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"),
    "C": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
}


class UnsupportedGlyph(KeyError):
    """The typeface only covers what the title needs."""


def text_width(text: str) -> int:
    return len(text) * (GLYPH_WIDTH + GLYPH_SPACING) - GLYPH_SPACING


def rasterize(text: str) -> list[str]:
    """Turns a string into a pixel grid, row by row."""
    glyphs = []
    for character in text.upper():
        try:
            glyphs.append(_GLYPHS[character])
        except KeyError:
            raise UnsupportedGlyph(
                f"The pixel typeface has no {character!r}. "
                f"Available: {''.join(sorted(_GLYPHS))}"
            ) from None

    return [
        (" " * GLYPH_SPACING).join(glyph[row] for glyph in glyphs)
        for row in range(GLYPH_HEIGHT)
    ]


def render(text: str, colour: str, shadow: str | None = None) -> list[Text]:
    """Draws the text with half blocks, two pixel rows per line.

    With `shadow`, odd pixels use a darker tone: it adds depth without doubling
    the height, which is what keeps a pixel title from looking flat.
    """
    grid = rasterize(text)
    if len(grid) % 2:
        grid = [*grid, " " * len(grid[0])]

    lines: list[Text] = []
    for index in range(0, len(grid), 2):
        top, bottom = grid[index], grid[index + 1]
        line = Text()
        for column in range(len(top)):
            upper = top[column] == "#"
            lower = bottom[column] == "#"
            upper_colour = colour
            lower_colour = shadow if shadow and index + 1 >= len(grid) - 2 else colour
            if upper and lower:
                line.append("▀", Style(color=upper_colour, bgcolor=lower_colour))
            elif upper:
                line.append("▀", Style(color=upper_colour))
            elif lower:
                line.append("▄", Style(color=lower_colour))
            else:
                line.append(" ")
        lines.append(line)
    return lines
