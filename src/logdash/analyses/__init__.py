"""Analysis modules. Add a module name to ``BUILTIN`` to register it."""

from __future__ import annotations

import importlib

BUILTIN: tuple[str, ...] = ("overview", "distributions")


def load_builtin() -> None:
    """Import the built-in analysis modules so their ``@register`` decorators run."""
    for name in BUILTIN:
        importlib.import_module(f"{__name__}.{name}")
