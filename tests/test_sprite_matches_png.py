"""The committed sprite has to be the original PNG, untouched.

The mascot is drawn outside the repository. `_sprite_data.py` is generated from
the PNG, so it is read again here and compared: if anyone tweaks a pixel by
hand in the generated module — or changes the PNG without regenerating it —
this fails.

It needs Pillow, which is not a dependency of the tool. Without it, it skips.
"""

from pathlib import Path

import pytest

from opaas_bench.cli.sprite import PALETTE, SURICATA

pytest.importorskip("PIL", reason="Pillow is only needed to regenerate the sprite")
pytest.importorskip("numpy", reason="numpy is only needed to regenerate the sprite")

ROOT = Path(__file__).resolve().parents[1]
PNG = ROOT / "docs" / "img" / "suricata.png"


@pytest.fixture(scope="module")
def extracted():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from sprite_from_png import extract
    finally:
        sys.path.pop(0)
    return extract(PNG)


def test_the_png_is_committed():
    """Without the original there is no way to check that nobody has edited it."""
    assert PNG.exists(), f"the original is missing at {PNG.relative_to(ROOT)}"


def test_the_grid_is_the_png(extracted):
    _, rows = extracted
    assert rows == SURICATA, (
        "the sprite and the PNG do not match; regenerate with: "
        "python scripts/sprite_from_png.py docs/img/suricata.png"
    )


def test_the_palette_is_the_png(extracted):
    palette, _ = extracted
    assert palette == PALETTE
