"""Meerkat grid. Generated, do not edit by hand.

Output of `python scripts/sprite_from_png.py docs/img/mascot.png`. The mascot
is read from the original PNG without retouching it, and a test reads it
again to check that what is here is still that image.
"""

from __future__ import annotations

#: Colors exactly as they come out of the PNG, from darkest to lightest.
PALETTE: dict[str, str] = {
    "K": "#392418",  # 86 cells
    "E": "#6a4027",  # 143 cells
    "D": "#945b34",  # 155 cells
    "M": "#ab7045",  # 242 cells
    "T": "#c38851",  # 24 cells
    "L": "#daa266",  # 258 cells
    "B": "#e3b57f",  # 13 cells
    "W": "#fddeb3",  # 199 cells
    "S": "#fcfbfc",  # 1 cells
}

#: 40 columns × 60 rows.
MASCOT: tuple[str, ...] = (
    "......DDDDDDDDDD........................",
    "......DDDDDDDDDD........................",
    "....DDWWWWWWWWLLDD......................",
    "....DDWWWWWWWWLLDD......................",
    "....WWWWMMMMWWWWLLDD....................",
    "..DDWWMMKKKKDDWWLLKKK...................",
    "..DDWWMMKSKKDDWWLLKKKK..................",
    "KKWWWWLMKKKKWWWWKKKKKK..................",
    "KKWWWWLLKKKKWWWWKKKKKK..................",
    "EELLLLWWWWWWWWWWDDKKKK..................",
    "EELLLLWWWWWWWWWWDDKKK...................",
    "..DDDDDDWWWWWWDDDDEE....................",
    "......LLLLLLDDDDDD......................",
    "......LLLLLLDDDDDD......................",
    "........DDLLWWWWDD......................",
    "........DDLLWWWWDD......................",
    "........DDWWWWWWDD......................",
    "........LLWWWWWLMM......................",
    "......DDLLWWWWLLLLDD....................",
    "......DDWWWWLLLLLLMM....................",
    "......DDWWWWLLLLLLMM....................",
    "......DDWWWWLLLLLLMMEE..................",
    "......DDWWWWLLLLLLMMEE..................",
    "....DDWWWWWWDDLLLLMMEE..................",
    "....DDWWWWWWDDLLMMEEEE..................",
    "....DDWWWWWWDDLLMMEEEE..................",
    "....DDLLWWTTLLLLMMMMEE..................",
    "....DDLLWWTTLLLLMMMMEE..................",
    "..DDLLLLWWTTLLMMEEMMEE..................",
    "..DDLLMMWWLLLLMMEEMMMEE.................",
    "..DDLLMMWWLLLLMMEEMMMME.................",
    "..DDLLMMWWLLMMEEMMLLMME.................",
    "..DDLLMMWWLLMMEEMMLLMME.................",
    "..KKMMEEWWMMMMEELLLLLLE.................",
    "...KDDEEWWMMMMDDLLLLLME.................",
    "....KKEEWWKKMMLLLLLLMME.................",
    "....KKKKWWKKKKWWLLLLMME.................",
    "....KKKKWWKKKKWWLLLLMME.................",
    "......DDWWLLWWWWLLMMMMMEE...............",
    "......DDWWBLWWWWLLMMMMMEE...............",
    "......DDWWWWWWWWLLLLMMMEE...............",
    "......MTBBWWWWLLLLLLMMMEE...............",
    "......MTBBWWWWLLLLLLMMMEE...............",
    "....DDLLBBWWWWTTLLLLMMMEE...............",
    "....DDLLBBWWWWTTLLLLMMMEE...............",
    "....DDLLTTWWBBTTLLLLLLMEE...............",
    "....DDLLTTWWBBTTLLLLMMMMMEE.............",
    "....DDLLLLMMMMLLLLLLMMMMMEE.............",
    "....DDLLLLMMMMLLLLLLMMEMMEE.............",
    "....DDMMLLMMMMLLLLMMMMEMMMMEE...........",
    "....DDMMLLMMMMLLLLMMMMEMMMMEE...........",
    "......DDLLMMEELLLLMMMMEMMMMMME..........",
    "......DDLLMMEELLLLMMMMEMMMMMMEE.........",
    "........MMMM..DDLLMMMMEEEMMMMMEE........",
    "........EEMM....DDLLMM.EEEEMMMMMEEE.....",
    "........EEMM....DDLLMM.EEEEMMMMMEEE.....",
    "......DDLLMM....DDLLMM...EEEEMMMMMMKKKKK",
    "....EELLLLMM..EELLLLMM.......EEEEEEKKK..",
    "....EEMDTLMM..EETTLTM........EEEEEEKKK..",
    "...KEEKKMM..KKEEKKMM....................",
)
