"""Packaged read-only resources (F09, design A9.3).

All frozen/checked-out reads of bundled text go through
:func:`read_resource_text` so engine config, Short-Lab defaults, identity
tables and SQL migrations resolve identically in source and in the
PyInstaller bundle.

Lookup order:

1. ``importlib.resources.files("diveintocrypto_desktop")`` — works in a
   source checkout, an installed wheel and (when ``datas`` are collected)
   inside a frozen bundle.
2. ``sys._MEIPASS`` fallback — only exercised by the frozen product smoke
   (read-only install dir, no source checkout). Unit tests never fake it
   into a pass; see ``docs/packaging.md``.

``relative_name`` is always slash-separated and relative to the
``diveintocrypto_desktop`` package root, e.g.
``"shortlab/default.yaml"`` or ``"shortlab/migrations/001_init.sql"``.
Absolute paths and ``..`` are rejected so callers cannot escape the
package.
"""

from __future__ import annotations

import pathlib
import sys

_PACKAGE = "diveintocrypto_desktop"


def _normalize(relative_name: str) -> str:
    if not isinstance(relative_name, str) or not relative_name.strip():
        raise ValueError("relative_name must be a non-empty str")
    text = relative_name.strip().replace("\\", "/")
    while text.startswith("/"):
        text = text[1:]
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"relative_name escapes the package: {relative_name!r}")
    return "/".join(parts)


def read_resource_text(relative_name: str) -> str:
    """Return the packaged text resource ``relative_name``.

    Unified ``importlib.resources`` entry point for
    ``engine/loader.py``, ``shortlab/config.py``,
    ``shortlab/repository.py`` and ``shortlab/identity/overrides.py``.
    The ``_MEIPASS`` fallback below is intentionally uncovered by unit
    tests — it is verified by the frozen product smoke only.
    """
    rel = _normalize(relative_name)
    try:
        from importlib import resources as _resources

        anchor = _resources.files(_PACKAGE)
        target = anchor
        for part in rel.split("/"):
            target = target.joinpath(part)
        # ``is_file`` guards directory traversal on case-insensitive filesystems.
        if target.is_file():
            return target.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    except Exception:
        # Any other importlib failure falls through to _MEIPASS below so a
        # frozen layout without importlib metadata still boots from resources.
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = pathlib.Path(str(meipass))
        for candidate in (
            base / _PACKAGE / rel,
            base / rel,
        ):
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8")
            except OSError:
                continue
    # Final filesystem fallback for development checkouts where the package
    # is not installed (tests run from ``src/`` via ``pythonpath``): resolve
    # relative to this file's package root. Frozen bundles never reach here
    # without _MEIPASS, so this does not mask a missing spec ``datas`` entry.
    try:
        here = pathlib.Path(__file__).resolve()
        # .../diveintocrypto_desktop/resources.py -> .../diveintocrypto_desktop
        root = here.parent
        candidate = root / rel
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except OSError:
        pass
    raise FileNotFoundError(f"packaged resource not found: {rel!r}")
