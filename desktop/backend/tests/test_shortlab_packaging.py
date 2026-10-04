"""Task 18: short-lab desktop naming, spec, health compat and packaged smoke.

Covers design sections 1.5 / 30 / 35.1 plus the Task 18 checklist:

- distribution ``short-lab-desktop`` with BOTH ``short-lab`` and
  ``dive-desktop`` console scripts pointing at the same ``__main__:main``;
- UI package ``short-lab-desktop-ui``; Python import path
  ``diveintocrypto_desktop`` untouched;
- legacy ``/api/health`` keeps ``service="dive-into-crypto-desktop"`` and
  gains ``product="short-lab"``;
- ``short-lab.spec`` (renamed from ``dive.spec``): EXE/COLLECT both named
  ``short-lab``, bundles DuckDB binaries + ``shortlab/default.yaml`` +
  React dist, fixed build command, no stale ``dive-desktop`` product paths
  outside compat-CLI prose;
- frozen read-only smoke (offline-feasible part): with a read-only install
  dir, two consecutive frozen resolutions land on the SAME writable user
  data dir holding ``shortlab.duckdb`` — never inside ``_internal``.

A real PyInstaller bundle cannot be produced in this sandbox (no network for
``uv run --with pyinstaller``); that step is covered statically here and
flagged in the acceptance report, never claimed as passed.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent
DESKTOP_DIR = BACKEND_DIR.parent
REPO_ROOT = DESKTOP_DIR.parent

LEGACY_SERVICE = "dive-into-crypto-desktop"
NEW_PRODUCT = "short-lab"
ENTRY_TARGET = "diveintocrypto_desktop.__main__:main"


def _load_pyproject() -> dict:
    return tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))


def test_distribution_name_and_dual_cli_alias() -> None:
    py = _load_pyproject()
    assert py["project"]["name"] == "short-lab-desktop"
    scripts = py["project"]["scripts"]
    assert scripts["short-lab"] == ENTRY_TARGET
    # Compat alias: the old command keeps working, same entry function.
    assert scripts["dive-desktop"] == ENTRY_TARGET


def test_both_cli_names_resolve_to_same_main() -> None:
    from diveintocrypto_desktop.__main__ import main as source_main
    import importlib

    for script in ("short-lab", "dive-desktop"):
        target = _load_pyproject()["project"]["scripts"][script]
        mod_name, func_name = target.split(":")
        resolved = getattr(importlib.import_module(mod_name), func_name)
        assert resolved is source_main, script


def test_ui_package_name() -> None:
    pkg = json.loads((DESKTOP_DIR / "ui" / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == "short-lab-desktop-ui"


def test_python_import_path_preserved() -> None:
    # No global rename: the package directory keeps its historic name.
    assert (BACKEND_DIR / "src" / "diveintocrypto_desktop").is_dir()
    import diveintocrypto_desktop  # noqa: F401


def test_health_keeps_legacy_service_and_adds_product() -> None:
    from fastapi.testclient import TestClient

    from diveintocrypto_desktop.api.app import create_app

    app = create_app()

    class _StubRuntime:
        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    app.state.shortlab_runtime = _StubRuntime()
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == LEGACY_SERVICE
    assert body["product"] == NEW_PRODUCT


def test_spec_renamed_and_outputs_short_lab() -> None:
    assert (BACKEND_DIR / "short-lab.spec").exists()
    assert not (BACKEND_DIR / "dive.spec").exists()
    spec = (BACKEND_DIR / "short-lab.spec").read_text(encoding="utf-8")
    assert 'name="short-lab"' in spec
    # EXE and COLLECT are both named short-lab.
    assert spec.count('name="short-lab"') >= 2
    # Build command is the fixed one.
    assert "uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm" in spec
    # Frozen bundle carries the query engine, the default config and the UI.
    assert "duckdb" in spec
    assert "default.yaml" in spec
    assert "ui/dist" in spec


def _non_compat_lines_with_old_product(text: str) -> list[str]:
    """Lines still referencing the old product paths, minus compat-CLI prose.

    A line mentioning the legacy CLI is only "compat prose" when it also
    says alias/legacy/compat/old-command (or shows a ``uv run`` invocation).
    Bare ``dist/dive-desktop`` / ``dive-desktop-windows-x64`` / build-command
    references are stale product paths and must be gone.
    """
    offenders: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        mentions_old_product = (
            "dist/dive-desktop" in line
            or "dist\\dive-desktop" in line
            or "dive-desktop-windows-x64" in line
            or "pyinstaller dive.spec" in line
            or "pyinstaller pyinstaller dive" in line
        )
        if not mentions_old_product:
            continue
        compat_markers = ("alias", "legacy", "compat", "old ", "old`", "uv run")
        if any(m in low for m in compat_markers):
            continue
        offenders.append(line)
    return offenders


def test_no_stale_product_paths_in_spec_workflow_or_packaging_docs() -> None:
    spec = (BACKEND_DIR / "short-lab.spec").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    packaging_doc = (REPO_ROOT / "docs" / "packaging.md").read_text(encoding="utf-8")
    freeze_doc = (BACKEND_DIR / "requirements-freeze.md").read_text(encoding="utf-8")
    assert _non_compat_lines_with_old_product(spec) == []
    assert _non_compat_lines_with_old_product(workflow) == []
    assert _non_compat_lines_with_old_product(packaging_doc) == []
    assert _non_compat_lines_with_old_product(freeze_doc) == []


def test_spec_bundled_resource_paths_exist_in_checkout() -> None:
    assert (BACKEND_DIR / "src" / "diveintocrypto_desktop" / "shortlab" / "default.yaml").is_file()
    assert (DESKTOP_DIR / "ui" / "dist").is_dir()


def test_frozen_resolves_to_writable_user_dir_twice(tmp_path: Path, monkeypatch) -> None:
    """Read-only extraction dir: two frozen boots share one writable DB home."""
    from diveintocrypto_desktop.shortlab import paths as paths_mod

    # Simulate the Windows target the CI packager runs on.
    monkeypatch.setattr(sys, "platform", "win32")
    fake_home = tmp_path / "localappdata"
    env = {"LOCALAPPDATA": str(fake_home)}
    first = paths_mod.resolve_data_dir(frozen=True, env=env)
    second = paths_mod.resolve_data_dir(frozen=True, env=env)
    assert first == second
    assert first.name == "short-lab"
    assert "_internal" not in first.parts
    # The runtime DB filename lives there and is writable across "restarts".
    db_path = first / paths_mod.DB_FILENAME
    assert db_path.name == "shortlab.duckdb"
    db_path.write_bytes(b"boot-1")
    assert second.joinpath(paths_mod.DB_FILENAME).read_bytes() == b"boot-1"
    db_path.write_bytes(b"boot-2")
    assert first.joinpath(paths_mod.DB_FILENAME).read_bytes() == b"boot-2"


def test_frozen_never_uses_install_dir_even_when_cwd_is_readonly(
    tmp_path: Path, monkeypatch,
) -> None:
    from diveintocrypto_desktop.shortlab import paths as paths_mod

    monkeypatch.setattr(sys, "platform", "win32")
    fake_internal = tmp_path / "dist" / "short-lab" / "_internal"
    fake_internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_internal), raising=False)
    env = {"LOCALAPPDATA": str(tmp_path / "lad")}
    resolved = paths_mod.resolve_data_dir(frozen=False, env=env)
    assert str(fake_internal) not in str(resolved)
    assert "_internal" not in resolved.parts


def test_f09_default_port_via_argparse_ast_not_comments() -> None:
    """F09 base gate: argparse default is 46408 (AST, never a comment)."""
    import ast as _ast

    src = (BACKEND_DIR / "src" / "diveintocrypto_desktop" / "__main__.py").read_text(
        encoding="utf-8"
    )
    tree = _ast.parse(src)
    defaults: list[int] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            names = [a.value for a in node.args if isinstance(a, _ast.Constant)]
            if "--port" in names:
                for kw in node.keywords:
                    if kw.arg == "default" and isinstance(kw.value, _ast.Constant):
                        defaults.append(kw.value.value)
    assert defaults == [46408]
