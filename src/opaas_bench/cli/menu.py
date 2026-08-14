"""Navigable cover-page menu.

No new dependencies: reading keys is thirty lines per operating system, and
pulling a TUI library into a tool whose selling point is being auditable and
lightweight would be a bad trade.

The menu **only appears with an interactive terminal**. Piped to a file, in CI
or with output redirected, it falls back to the usual help: a certification
tool is run from scripts more often than by hand.

No entry hides the CLI. Read-only ones are executed; the ones that need
arguments print the exact command to copy, which teaches you to use the tool
rather than replacing it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.style import Style
from rich.text import Text

from opaas_bench.cli.banner import PALETTE

UP, DOWN, ENTER, QUIT, OTHER = "up", "down", "enter", "quit", "other"


@dataclass(slots=True)
class MenuItem:
    icon: str
    label: str
    #: Command printed for copying. Empty if the action is executed.
    command: str = ""
    #: Argument-less action executed when the entry is chosen.
    action: Callable[[], None] | None = None
    quit: bool = False


@dataclass
class Menu:
    """Menu state, kept apart from input and output so it can be tested."""

    items: Sequence[MenuItem]
    index: int = 0
    title: str = "What would you like to do?"

    def move(self, delta: int) -> None:
        self.index = (self.index + delta) % len(self.items)

    @property
    def selected(self) -> MenuItem:
        return self.items[self.index]

    def render(self) -> RenderableType:
        lines: list[Text] = [
            Text(f"  {self.title}", Style(color=PALETTE.muted)),
            Text(),
        ]
        for position, item in enumerate(self.items):
            chosen = position == self.index
            line = Text()
            line.append(" ▸ " if chosen else "   ", Style(color=PALETTE.accent))
            line.append(f"{item.icon}  ", Style(color=PALETTE.accent if chosen else PALETTE.muted))
            line.append(
                item.label,
                Style(color=PALETTE.text, bold=chosen)
                if chosen
                else Style(color=PALETTE.muted),
            )
            lines.append(line)
        lines.append(Text())
        lines.append(
            Text(
                "  ↑↓ move · Enter select · q quit",
                Style(color=PALETTE.muted, italic=True),
            )
        )
        return Group(*lines)


def is_interactive() -> bool:
    """Without a terminal on both ends, a menu cannot work."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def read_key() -> str:
    """One keystroke, normalized. It always restores the terminal state."""
    if os.name == "nt":
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            return {"H": UP, "P": DOWN}.get(msvcrt.getwch(), OTHER)
        if char in ("\r", "\n"):
            return ENTER
        if char in ("\x03", "\x1b", "q", "Q"):
            return QUIT
        return OTHER

    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        char = sys.stdin.read(1)
        if char == "\x1b":
            following = sys.stdin.read(2)
            return {"[A": UP, "[B": DOWN}.get(following, QUIT)
        if char in ("\r", "\n"):
            return ENTER
        if char in ("\x03", "q", "Q"):
            return QUIT
        return OTHER
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def choose(
    console: Console,
    items: Sequence[MenuItem],
    *,
    key_reader: Callable[[], str] = read_key,
) -> MenuItem | None:
    """Draws the menu and returns the chosen entry, or None if cancelled."""
    from rich.live import Live

    menu = Menu(items)
    with Live(menu.render(), console=console, auto_refresh=False, transient=True) as live:
        while True:
            try:
                key = key_reader()
            except KeyboardInterrupt:
                return None
            if key == QUIT:
                return None
            if key == ENTER:
                return menu.selected
            if key in (UP, DOWN):
                menu.move(-1 if key == UP else 1)
                live.update(menu.render(), refresh=True)


def present(console: Console, items: Sequence[MenuItem]) -> None:
    """Picks an entry and either runs it or shows its command."""
    item = choose(console, items)
    if item is None or item.quit:
        return
    if item.action is not None:
        console.print()
        item.action()
        return
    console.print()
    console.print(Text("  Run:", Style(color=PALETTE.muted)))
    console.print(Text(f"  {item.command}", Style(color=PALETTE.accent)))
