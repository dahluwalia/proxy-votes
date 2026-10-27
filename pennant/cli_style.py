"""How the `pennant` commands look: one console, colours for votes and facts, tables.

Design choice: every command prints through `rich` (already installed with Typer), so
tables size to the terminal and votes are colour-coded: FOR green, AGAINST red, REVIEW
yellow; yes / no / unknown facts show as ✓ / ✗ / ?. Colour is only added on a terminal:
piped or captured output (tests, `> file`) is plain text, 160 columns wide so tables
don't wrap.
"""

from __future__ import annotations

import sys
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

VOTE_STYLE = {"FOR": "bold green", "AGAINST": "bold red", "REVIEW": "bold yellow",
              "MISSING": "bold magenta"}
TRI_MARK = {"yes": ("✓", "green"), "no": ("✗", "dim"), "unknown": ("?", "yellow")}
GATE_STYLE = {"pass": "green", "FAIL": "bold red", "skipped": "dim"}
STATUS_STYLE = {"confirmed": "green", "past": "dim", "stale": "yellow", "contradiction": "red",
                "undisclosed": "cyan", "ch_invisible": "blue", "ch_unavailable": "red"}
RUN_STYLE = {"published": "bold green", "staged": "green", "held": "bold yellow",
             "failed": "bold red", "running": "yellow"}


def console() -> Console:
    """A console on the current stdout (so test runners capture it), full width on a
    terminal and 160 columns otherwise."""
    tty = sys.stdout.isatty()
    return Console(file=sys.stdout, highlight=False, soft_wrap=False,
                   width=None if tty else 160, force_terminal=None if tty else False)


def out(*parts: Any, **kwargs: Any) -> None:
    console().print(*parts, **kwargs)


def vote(v: str | None) -> Text:
    v = v or "MISSING"
    return Text(v, style=VOTE_STYLE.get(v, ""))


def tri(v: Any) -> Text:
    mark, style = TRI_MARK.get(str(v), (str(v), ""))
    return Text(mark, style=style)


def styled(text: str, style_map: dict[str, str]) -> Text:
    return Text(text, style=style_map.get(text, ""))


def table(title: str | None = None, *columns: str, caption: str | None = None) -> Table:
    """A borderless table with a bold header and a left-aligned title."""
    t = Table(title=title, caption=caption, box=box.SIMPLE_HEAD, header_style="bold",
              title_justify="left", caption_justify="left", title_style="bold",
              caption_style="dim", pad_edge=False, show_edge=False, padding=(0, 1),
              collapse_padding=True)
    for c in columns:
        t.add_column(c, overflow="fold")  # long ids and URLs fold; words wrap whole
    return t


def heading(text: str) -> None:
    out(Text(text, style="bold underline"))


def dim(text: str) -> Text:
    return Text(text, style="dim")


def counts(values: list[str]) -> Text:
    """'7 FOR · 1 AGAINST · 3 REVIEW', each in its vote colour."""
    t = Text()
    for v in ("FOR", "AGAINST", "REVIEW", "MISSING"):
        n = values.count(v)
        if n:
            if t:
                t.append(" · ")
            t.append(f"{n} {v}", style=VOTE_STYLE[v])
    return t


def changes(text: str) -> Text:
    """Diff or history text with vote words in their colours and arrows bold."""
    t = Text(text)
    for v, style in VOTE_STYLE.items():
        t.highlight_regex(rf"\b{v}\b", style)
    t.highlight_regex(r"→|->", "bold")
    t.highlight_regex(r"\(dec:\d+[^)]*\)|\(cache:[^)]*\)|\(policy[^)]*\)", "cyan")
    return t
