"""Which model reads a notice. Every backend answers the same task the same way.

Design choice: the pipeline sees only `NoticeExtract` (extract/schema.py): the meeting,
the roster, each director's appointments, and the page and text each field was cited
from. M1.2 then grounds every quote on the PDF page itself, so any backend is held to the
same standard and none is trusted. A backend is a module with:

- `NAMESPACE`: its `core.cache` namespace.
- `notice_key(pdf_bytes) -> str`: the cache key of one answer, covering the PDF, the
  schema version, the model and its configuration (prompts included).
- `extract_notice(pdf_path, ctx) -> NoticeExtract | None`: the extract, through
  `core.cache`; None when offline with nothing stored (callers treat that as unknown).

To plug in another model, write such a module, add it to BACKENDS and set
`EXTRACT_MODEL` in `.env`. Prompts go in this package's prompts.md.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from pennant.core.config import Settings

BACKENDS = {"llamaextract": "pennant.extract.llamaextract"}
DEFAULT = "llamaextract"


def backend(settings: Settings | None = None) -> ModuleType:
    """The backend `EXTRACT_MODEL` names (default LlamaExtract). An unknown name is an
    error, never a silent fallback to another model."""
    name = (getattr(settings, "extract_model", None) or DEFAULT).split(":", 1)[0]
    if name not in BACKENDS:
        raise ValueError(f"EXTRACT_MODEL={name!r}: no such extract backend; "
                         f"choose one of {sorted(BACKENDS)}")
    return importlib.import_module(BACKENDS[name])


def all_backends() -> list[ModuleType]:
    return [importlib.import_module(m) for m in BACKENDS.values()]
