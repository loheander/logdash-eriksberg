"""Anomaly detectors. Every module in this package is imported, so a new file is enough."""

from __future__ import annotations

import importlib
import pkgutil


def load_builtin() -> None:
    """Import every detector module so that its ``@register`` decorator runs."""
    for module in pkgutil.iter_modules(__path__):
        if module.name != "base" and not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")
