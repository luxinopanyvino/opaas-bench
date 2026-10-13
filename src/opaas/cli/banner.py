"""CLI cover page: mascot, title and the real state of the environment.

Four layouts depending on terminal size, because a cover page that breaks apart
looks like a bug:

- **full**: sprite, pixel title and status, inside a frame
- **medium**: sprite with text beside it, no large title
- **narrow**: everything stacked
- **compact**: no drawing

The sprite is the original image as is. When it does not fit whole it is
neither cropped nor shrunk — that would be editing it — it falls back to the
compact layout instead.

The status is not decorative. Every line checks something about the environment
and says what is missing, which is what you want to know before launching an
evaluation that can take an hour.
"""

from __future__ import annotations

from rich.box import SQUARE
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from opaas import OPAAS_SPEC_VERSION, __version__
from opaas.cli import pixelfont
from opaas.cli.sprite import HEIGHT_IN_LINES as SPRITE_HEIGHT
from opaas.cli.sprite import TAGLINE
from opaas.cli.sprite import WIDTH as SPRITE_WIDTH
from opaas.cli.sprite import render as render_sprite

TITLE = "OPAAS"
SUBTITLE = "On-Prem AI Audit Suite"

class _Palette:
    accent = "#c8873f"
    accent_dim = "#8a6134"
    text = "#d6cdc0"
    muted = "#6f6a63"
    ok = "#8fae6a"
    pending = "#c8873f"
    frame = "#7a5228"


PALETTE = _Palette()

_GAP = 3
_TITLE_WIDTH = pixelfont.text_width(TITLE)
#: Width taken by the longest status line.
_STATUS_WIDTH = 58
#: The title goes **above** the sprite, not beside it. Beside it, an
#: eleven-letter pixel title would push the minimum to 104 columns and almost
#: nobody would see the full cover; above, the widest block sets the
#: requirement.
MIN_FULL = max(_TITLE_WIDTH, SPRITE_WIDTH + _GAP + _STATUS_WIDTH) + 6
MIN_MEDIUM = SPRITE_WIDTH + _GAP + 46
#: Below this the drawing does not fit widthwise and it falls back to compact.
MIN_SPRITE = SPRITE_WIDTH

#: Minimum heights. The frame and the large title need twelve lines more than
#: the drawing; stacked, a couple of lines of slack is enough.
MIN_HEIGHT_FULL = SPRITE_HEIGHT + 12
MIN_HEIGHT_SPRITE = SPRITE_HEIGHT + 2


def status_lines() -> list[tuple[str, str, str]]:
    """Environment status: (mark, text, style). It checks, it does not decorate."""
    from opaas.bench.classifiers import classifiers_available
    from opaas.bench.datasets import available_sources, cache_dir, source_is_downloaded
    from opaas.bench.suites import BLOCKS, available_suites

    lines: list[tuple[str, str, str]] = [
        (
            "✓",
            f"{len(available_suites())} of {len(BLOCKS)} blocks implemented "
            f"({', '.join(sorted(BLOCKS))})",
            PALETTE.ok,
        )
    ]

    # Downloaded, not converted: a source used inside a composition never gets
    # its own directory, and the cover page saying "not downloaded" about the
    # data being evaluated was simply wrong.
    cached = [name for name in available_sources() if source_is_downloaded(name)]
    if cached:
        lines.append(("✓", f"datasets cached: {', '.join(cached)}", PALETTE.ok))
    else:
        lines.append(
            ("○", "datasets not downloaded — fetched on first use", PALETTE.pending)
        )

    if classifiers_available():
        from opaas.bench.classifiers import DEFAULT_CLASSIFIERS, is_default_classifier_cached

        ready = [task for task in DEFAULT_CLASSIFIERS if is_default_classifier_cached(task)]
        if len(ready) == len(DEFAULT_CLASSIFIERS):
            lines.append(("✓", "classifiers ready: faithfulness, toxicity", PALETTE.ok))
        elif ready:
            lines.append(
                ("○", f"classifiers ready: {', '.join(ready)} — the rest download on "
                 "first use", PALETTE.pending)
            )
        else:
            lines.append(
                ("○", "classifiers installed — weights download on first use",
                 PALETTE.pending)
            )
    else:
        lines.append(
            ("○", "no classifiers — faithfulness and toxicity unmeasured", PALETTE.pending)
        )

    lines.append(
        ("·", f"v{__version__} · methodology {OPAAS_SPEC_VERSION}", PALETTE.muted)
    )
    lines.append(("·", f"cache: {cache_dir()}", PALETTE.muted))
    return lines


def _status_block() -> list[Text]:
    rows = []
    for mark, message, colour in status_lines():
        line = Text()
        line.append(f"{mark} ", Style(color=colour, bold=mark == "✓"))
        line.append(message, Style(color=PALETTE.text if mark != "·" else PALETTE.muted))
        rows.append(line)
    return rows


def _rule(width: int) -> Text:
    return Text("─" * width, Style(color=PALETTE.frame))


def _full(width: int) -> RenderableType:
    """Pixel title on top; below it, the sprite and the environment status."""
    sprite = render_sprite()
    status = _status_block()
    inner = min(width - 6, max(_TITLE_WIDTH, SPRITE_WIDTH + _GAP + _STATUS_WIDTH))

    body: list[RenderableType] = list(pixelfont.render(TITLE, PALETTE.accent, PALETTE.accent_dim))
    body.append(Text(SUBTITLE, Style(color=PALETTE.text)))
    body.append(Text(TAGLINE, Style(color=PALETTE.muted, italic=True)))
    body.append(_rule(inner))
    body.append(Text())

    # The status is centered vertically against the sprite: pinned to the top it
    # would look like a stray caption.
    padding = max(0, (len(sprite) - len(status)) // 2)
    aligned = [Text()] * padding + status
    for index in range(len(sprite)):
        line = Text()
        line.append_text(sprite[index])
        line.append(" " * _GAP)
        if index < len(aligned):
            line.append_text(aligned[index])
        body.append(line)

    return Panel(
        Group(*body),
        # Square corners, not Rich's rounded default: everything above is drawn
        # on a pixel grid — the chunky sprite and the pixel title — and a
        # rounded corner is the only soft edge in the whole cover.
        box=SQUARE,
        border_style=Style(color=PALETTE.frame),
        padding=(1, 2),
        expand=False,
    )


def _medium() -> RenderableType:
    sprite = render_sprite()
    right = [
        Text(),
        Text("opaas", Style(color=PALETTE.accent, bold=True)),
        Text(TAGLINE, Style(color=PALETTE.muted, italic=True)),
        Text(),
        *_status_block(),
    ]
    lines = []
    for index in range(len(sprite)):
        line = Text()
        line.append_text(sprite[index])
        line.append(" " * _GAP)
        if index < len(right):
            line.append_text(right[index])
        lines.append(line)
    return Group(*lines)


def _narrow() -> RenderableType:
    return Group(
        *render_sprite(),
        Text(),
        Text("opaas", Style(color=PALETTE.accent, bold=True)),
        Text(TAGLINE, Style(color=PALETTE.muted, italic=True)),
        Text(),
        *_status_block(),
    )


def _compact() -> RenderableType:
    """No drawing: the terminal is not big enough to show it whole."""
    return Group(
        Text("opaas", Style(color=PALETTE.accent, bold=True)),
        Text(SUBTITLE, Style(color=PALETTE.text)),
        Text(),
        *_status_block(),
    )


def banner(width: int = 100, height: int | None = None) -> RenderableType:
    """Picks the layout that fits. `height=None` means "unknown, does not matter"."""
    fits = width >= MIN_SPRITE and (height is None or height >= MIN_HEIGHT_SPRITE)
    if not fits:
        return _compact()
    if width >= MIN_FULL and (height is None or height >= MIN_HEIGHT_FULL):
        return _full(width)
    if width >= MIN_MEDIUM:
        return _medium()
    return _narrow()


def print_banner(console: Console) -> None:
    # Height only matters in a real terminal: when redirecting to a file nothing
    # can overflow off the top.
    height = console.height if console.is_terminal else None
    console.print(banner(console.width, height))
