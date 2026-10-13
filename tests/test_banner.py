from pathlib import Path

import pytest
from rich.console import Console

from opaas.cli import pixelfont
from opaas.cli.banner import (
    MIN_FULL,
    MIN_HEIGHT_FULL,
    MIN_MEDIUM,
    MIN_SPRITE,
    SUBTITLE,
    TITLE,
    banner,
    print_banner,
    status_lines,
)
from opaas.cli.menu import Menu, MenuItem, choose, present
from opaas.cli.sprite import MASCOT, PALETTE, TAGLINE, render, render_svg


def _plain(width: int) -> str:
    console = Console(width=width, force_terminal=False, no_color=True)
    with console.capture() as capture:
        print_banner(console)
    return capture.get()


def _sized(width: int, height: int | None) -> str:
    console = Console(width=width, no_color=True)
    with console.capture() as capture:
        console.print(banner(width, height))
    return capture.get()


# ------------------------------------------------------------------- sprite


def test_every_row_has_the_same_width():
    """A short row shifts half the drawing with nothing warning you."""
    assert len({len(row) for row in MASCOT}) == 1


def test_the_grid_has_an_even_number_of_rows():
    assert len(MASCOT) % 2 == 0


def test_every_pixel_uses_a_known_colour():
    used = {pixel for row in MASCOT for pixel in row} - {"."}
    assert used <= set(PALETTE), f"undefined colours: {used - set(PALETTE)}"


def test_no_palette_entry_is_dead_weight():
    used = {pixel for row in MASCOT for pixel in row}
    assert set(PALETTE) <= used, f"colours defined and unused: {set(PALETTE) - used}"


def test_the_sprite_renders_one_line_per_two_rows():
    lines = render()
    assert len(lines) == len(MASCOT) // 2
    assert all(len(line.plain) == len(MASCOT[0]) for line in lines)


def test_transparent_pixels_paint_nothing():
    """With no background, the drawing looks the same on light and dark terminals."""
    assert render()[0].plain.startswith(" ")


def _luma(hex_colour: str) -> float:
    red, green, blue = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * red + 0.587 * green + 0.114 * blue


def test_the_eye_highlight_survives_the_grid():
    """A single highlight cell in the eye.

    It is the smallest feature of the original and the one that pins the grid at
    40×60: with a coarser one it would average out with the eye and the meerkat
    would come out with no gaze.
    """
    brightest = max(PALETTE, key=lambda letter: _luma(PALETTE[letter]))
    assert _luma(PALETTE[brightest]) > 240
    assert sum(row.count(brightest) for row in MASCOT) == 1


# ------------------------------------------------------------------ typeface


def test_the_pixel_font_covers_the_title():
    grid = pixelfont.rasterize(TITLE)
    assert len(grid) == pixelfont.GLYPH_HEIGHT
    assert len({len(row) for row in grid}) == 1
    assert len(grid[0]) == pixelfont.text_width(TITLE)


def test_an_unsupported_glyph_says_which_one():
    with pytest.raises(pixelfont.UnsupportedGlyph, match="'Z'"):
        pixelfont.rasterize("Z")


def test_the_font_renders_half_the_rows_as_lines():
    lines = pixelfont.render(TITLE, "#ffffff")
    assert len(lines) == 4  # 7 rows rounded up to 8


@pytest.mark.parametrize("letter", list("OPAAS"))
def test_every_letter_of_the_title_has_ink(letter):
    assert any("#" in row for row in pixelfont.rasterize(letter))


# ------------------------------------------------------------------- cover


def test_a_wide_terminal_uses_the_framed_layout():
    output = _plain(MIN_FULL + 10)
    assert "┌" in output and "┘" in output
    assert SUBTITLE in output


def test_a_medium_terminal_drops_the_frame():
    output = _plain(MIN_MEDIUM + 2)
    assert "┌" not in output
    assert "opaas" in output


def test_a_narrow_terminal_stacks_everything():
    output = _plain(MIN_SPRITE)
    line = next(line for line in output.splitlines() if "opaas" in line)
    assert line.strip() == "opaas"


def test_a_terminal_narrower_than_the_sprite_drops_it():
    """Rather than crop the drawing, do not show it."""
    output = _sized(MIN_SPRITE - 1, 200)
    assert "▀" not in output and "▄" not in output
    assert "opaas" in output and SUBTITLE in output


def test_a_short_terminal_drops_the_sprite_too():
    """At 24 lines the drawing would push everything else off screen."""
    output = _sized(200, 24)
    assert "▀" not in output and "▄" not in output
    assert "blocks implemented" in " ".join(output.split())


def test_a_tall_wide_terminal_still_gets_the_frame():
    assert "┌" in _sized(MIN_FULL + 10, MIN_HEIGHT_FULL)


def test_an_unknown_height_never_blocks_the_sprite():
    """When redirecting to a file there is no height to respect."""
    assert "▀" in _sized(MIN_FULL + 10, 200)
    assert "▀" in _sized(MIN_FULL + 10, None)


@pytest.mark.parametrize("width", [30, 45, 72, 90, 100, 140, 200])
def test_the_banner_never_wraps(width):
    """A line that wraps breaks the drawing and looks like a bug."""
    for line in _plain(width).splitlines():
        assert len(line.rstrip()) <= width


def test_the_tagline_is_present():
    assert TAGLINE in _plain(100)


# ------------------------------------------------------------------- status


def test_the_status_reports_the_five_blocks():
    text = " ".join(message for _, message, _ in status_lines())
    assert "5 of 5 blocks" in text


def test_the_status_says_what_is_missing_not_just_what_works():
    marks = {mark for mark, _, _ in status_lines()}
    assert marks <= {"✓", "○", "·"}
    assert "·" in marks, "the version and the cache are informational, not checks"


def test_the_status_mentions_the_cache_location():
    text = " ".join(message for _, message, _ in status_lines())
    assert "cache:" in text


def test_the_status_counts_sources_downloaded_inside_a_composition(monkeypatch):
    """A source used in a composition has no converted directory of its own, and
    the cover page was calling the data being evaluated 'not downloaded'."""
    import opaas.bench.datasets as datasets

    monkeypatch.setattr(datasets, "source_is_downloaded", lambda name: name == "ifeval")
    text = " ".join(message for _, message, _ in status_lines())
    assert "datasets cached: ifeval" in text

    monkeypatch.setattr(datasets, "source_is_downloaded", lambda name: False)
    text = " ".join(message for _, message, _ in status_lines())
    assert "not downloaded" in text


def _with_classifiers_available(monkeypatch, *, cached):
    import opaas.bench.classifiers as classifiers

    monkeypatch.setattr(classifiers, "classifiers_available", lambda: True)
    monkeypatch.setattr(classifiers, "is_default_classifier_cached", lambda task: task in cached)


def test_the_status_says_classifiers_are_unavailable_without_the_extra(monkeypatch):
    import opaas.bench.classifiers as classifiers

    monkeypatch.setattr(classifiers, "classifiers_available", lambda: False)
    text = " ".join(message for _, message, _ in status_lines())
    assert "no classifiers" in text
    assert "unmeasured" in text


def test_the_status_reports_which_default_classifiers_are_cached(monkeypatch):
    _with_classifiers_available(monkeypatch, cached=set())
    text = " ".join(message for _, message, _ in status_lines())
    assert "download on first use" in text

    _with_classifiers_available(monkeypatch, cached={"toxicity"})
    text = " ".join(message for _, message, _ in status_lines())
    assert "classifiers ready: toxicity" in text
    assert "download on first use" in text

    _with_classifiers_available(monkeypatch, cached={"toxicity", "faithfulness"})
    text = " ".join(message for _, message, _ in status_lines())
    assert "classifiers ready: faithfulness, toxicity" in text
    assert "download on first use" not in text


# --------------------------------------------------------------------- SVG


def test_the_committed_svg_matches_the_sprite():
    """The README SVG and the terminal drawing come from the same data."""
    committed = Path(__file__).resolve().parents[1] / "docs" / "img" / "mascot.svg"
    assert committed.read_text(encoding="utf-8") == render_svg(), (
        "regenera con: python -c \"import pathlib; from opaas.cli.sprite import "
        "render_svg; pathlib.Path('docs/img/mascot.svg').write_text(render_svg(), "
        'encoding=\'utf-8\', newline=\'\')"'
    )


def test_the_svg_has_no_external_dependencies():
    svg = render_svg()
    assert "<image" not in svg and "@import" not in svg
    assert "http://www.w3.org/2000/svg" in svg


# -------------------------------------------------------------------- menu


def _items():
    return [MenuItem("a", "One"), MenuItem("b", "Two"), MenuItem("c", "Three", quit=True)]


def test_the_selection_wraps_around():
    menu = Menu(_items())
    menu.move(-1)
    assert menu.selected.label == "Three"
    menu.move(1)
    assert menu.selected.label == "One"


def test_the_selected_entry_is_marked():
    menu = Menu(_items())
    menu.move(1)
    console = Console(width=60, no_color=True)
    with console.capture() as capture:
        console.print(menu.render())
    marked = [line for line in capture.get().splitlines() if "▸" in line]
    assert len(marked) == 1
    assert "Two" in marked[0]


def test_the_menu_shows_how_to_operate_it():
    console = Console(width=60, no_color=True)
    with console.capture() as capture:
        console.print(Menu(_items()).render())
    assert "Enter" in capture.get() and "quit" in capture.get()


def test_enter_returns_the_highlighted_entry():
    keys = iter(["down", "down", "enter"])
    chosen = choose(Console(width=60), _items(), key_reader=lambda: next(keys))
    assert chosen.label == "Three"


def test_q_cancels_without_choosing():
    keys = iter(["down", "quit"])
    assert choose(Console(width=60), _items(), key_reader=lambda: next(keys)) is None


def test_an_interrupt_cancels_cleanly():
    def _interrupt():
        raise KeyboardInterrupt

    assert choose(Console(width=60), _items(), key_reader=_interrupt) is None


def test_unknown_keys_are_ignored():
    keys = iter(["other", "other", "enter"])
    chosen = choose(Console(width=60), _items(), key_reader=lambda: next(keys))
    assert chosen.label == "One"


def _chooses(monkeypatch, *picks):
    """Feeds `present` a fixed sequence of choices, cancelling once they run out."""
    remaining = iter(picks)
    monkeypatch.setattr("opaas.cli.menu.choose", lambda *a, **k: next(remaining, None))


def test_an_entry_with_a_command_prints_it_instead_of_running_anything(monkeypatch):
    """The menu teaches the CLI, it does not replace it."""
    item = MenuItem("x", "Evaluate", command="opaas run --model X")
    _chooses(monkeypatch, item)
    console = Console(width=80, no_color=True)
    with console.capture() as capture:
        present(console, [item])
    assert "opaas run --model X" in capture.get()


def test_an_entry_with_an_action_executes_it(monkeypatch):
    executed = []
    item = MenuItem("x", "List", action=lambda: executed.append(True))
    _chooses(monkeypatch, item)
    present(Console(width=80, no_color=True), [item])
    assert executed == [True]


def test_quitting_does_nothing(monkeypatch):
    item = MenuItem("x", "Exit", command="never", quit=True)
    _chooses(monkeypatch, item)
    console = Console(width=80, no_color=True)
    with console.capture() as capture:
        present(console, [item])
    assert "never" not in capture.get()


def test_the_menu_comes_back_after_each_entry(monkeypatch):
    executed = []
    action = MenuItem("x", "List", action=lambda: executed.append("action"))
    command = MenuItem("y", "Evaluate", command="opaas run --model X")
    _chooses(monkeypatch, action, command, action)
    console = Console(width=80, no_color=True)
    with console.capture() as capture:
        present(console, [action, command])
    assert executed == ["action", "action"]
    assert "opaas run --model X" in capture.get()


def test_an_exit_entry_stops_the_menu(monkeypatch):
    executed = []
    action = MenuItem("x", "List", action=lambda: executed.append("action"))
    leave = MenuItem("z", "Exit", quit=True)
    _chooses(monkeypatch, action, leave, action)
    present(Console(width=80, no_color=True), [action, leave])
    assert executed == ["action"]
