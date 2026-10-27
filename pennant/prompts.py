"""Load the prompts kept in a package's prompts.md.

Design choice: prompts are documents, not code, so each package that talks to a model
keeps them in one Markdown file a reader can review: a `## <name>` heading, a line or two
on when it is sent, then the exact text in a ````text fence (four backticks, so a prompt
may itself contain ``` fences). The fence body is returned byte for byte; nothing is
reflowed, because a prompt's text can be part of a cache key.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

_SECTION = re.compile(r"^## (?P<name>\S+)[ \t]*\n.*?^````text\n(?P<body>.*?)^````[ \t]*$",
                      re.M | re.S)


@cache
def load_prompts(path: Path) -> dict[str, str]:
    """Every prompt in a prompts.md file, by section name."""
    found = {m["name"]: m["body"] for m in _SECTION.finditer(path.read_text())}
    if not found:
        raise ValueError(f"{path} has no `## name` sections with a ````text block")
    return found


def prompt(path: Path, name: str) -> str:
    try:
        return load_prompts(path)[name]
    except KeyError:
        raise KeyError(f"no prompt {name!r} in {path}") from None
