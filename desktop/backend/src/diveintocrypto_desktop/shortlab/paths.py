"""Short-Lab writable data directory resolution (design section 19).

Rules (in priority order):

1. ``SHORTLAB_DATA_DIR`` explicitly overrides everything. The path is
   created when missing and verified writable with a real probe file; it
   must never resolve inside a PyInstaller ``_internal`` resource tree.
2. Packaged runs (``frozen=True`` or ``sys.frozen`` / ``sys._MEIPASS``
   present) use the per-user data directory -- ``%LOCALAPPDATA%/short-lab``
   on Windows, ``~/Library/Application Support/short-lab`` on macOS,
   ``$XDG_DATA_HOME/short-lab`` (else ``~/.local/share/short-lab``) on
   Linux. The install directory (``sys._MEIPASS`` / ``_internal``) holds
   read-only resources and is never used for the DuckDB file.
3. Source runs default to ``desktop/backend/runtime`` (next to this
   package's backend root), which is git-ignored local data.

``sys.platform`` is read at call time so tests can monkeypatch it to
simulate Windows. ``HOME``/``LOCALAPPDATA``/``XDG_DATA_HOME`` are read
from the ``env`` mapping first so tests stay hermetic (falling back to
``os.environ`` only for keys absent from ``env``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

DB_FILENAME = "shortlab.duckdb"

_ENV_OVERRIDE = "SHORTLAB_DATA_DIR"


class DataDirNotWritableError(OSError):
    """The resolved Short-Lab data directory cannot be created/written."""


def _lookup(env: Mapping[str, str] | None, key: str) -> str | None:
    if env is not None and key in env:
        return env[key]
    return os.environ.get(key)


def _backend_dir() -> Path:
    # .../desktop/backend/src/diveintocrypto_desktop/shortlab/paths.py
    # -> parents: shortlab, diveintocrypto_desktop, src, backend
    return Path(__file__).resolve().parent.parent.parent.parent


def _default_source_data_dir() -> Path:
    return _backend_dir() / "runtime"


def is_packaged(frozen: bool) -> bool:
    """True for PyInstaller runs: explicit flag or interpreter markers."""
    if frozen:
        return True
    if getattr(sys, "frozen", False):
        return True
    return getattr(sys, "_MEIPASS", None) is not None


def _user_data_dir(env: Mapping[str, str] | None) -> Path:
    platform = sys.platform
    if platform.startswith("win"):
        local = _lookup(env, "LOCALAPPDATA")
        if not local:
            home = _lookup(env, "USERPROFILE") or os.path.expanduser("~")
            local = str(Path(home) / "AppData" / "Local")
        return Path(local) / "short-lab"
    if platform == "darwin":
        home = _lookup(env, "HOME") or os.path.expanduser("~")
        return Path(home) / "Library" / "Application Support" / "short-lab"
    xdg = _lookup(env, "XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "short-lab"
    home = _lookup(env, "HOME") or os.path.expanduser("~")
    return Path(home) / ".local" / "share" / "short-lab"


def _ensure_writable(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirNotWritableError(
            f"Short-Lab data directory is not creatable: {path} ({exc})"
        ) from exc
    if "_internal" in path.parts:
        raise DataDirNotWritableError(
            f"Short-Lab data directory must never live inside _internal: {path}"
        )
    probe = path / ".shortlab-write-test"
    try:
        probe.write_text("writable", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise DataDirNotWritableError(
            f"Short-Lab data directory is not writable: {path} ({exc})"
        ) from exc
    return path


def resolve_data_dir(
    frozen: bool = False,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve (creating when needed) the Short-Lab writable data directory."""
    override = _lookup(env, _ENV_OVERRIDE)
    if override:
        return _ensure_writable(Path(override).expanduser())
    if is_packaged(frozen):
        return _ensure_writable(_user_data_dir(env))
    return _ensure_writable(_default_source_data_dir())


def resolve_db_path(
    frozen: bool = False,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Default DuckDB file path for the given run mode."""
    return resolve_data_dir(frozen=frozen, env=env) / DB_FILENAME
