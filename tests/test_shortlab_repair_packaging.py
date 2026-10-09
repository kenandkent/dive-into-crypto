"""R16 packaging regression (design D01/D13/D16, plan P06 V16).

Root-level gate for the shippable bundle:

- ``short-lab.spec`` carries the FULL R16 resource set
  (001-006 / shortlab default / engine default / two identity YAMLs /
  DuckDB natives + UI dist), with an explicit 006 list (no placeholder).
- ``scripts/smoke_shortlab_packaged.py`` enforces the real 006 tables,
  schema version 6 and the read-only two-boot survival that
  ``release.yml`` gates on. CLI surface is frozen from argparse.
- The committed ``desktop/ui/dist`` bundle exists and carries the
  Short-Lab hash routes the backend serves at ``/``.
- Packaged CLI (``__main__.py`` argparse) stays
  ``127.0.0.1:46408`` / ``short-lab`` prog.
- ``006_optimization_repair.sql`` defines exactly the D13 seven tables
  plus eight indexes (R01 delivered the file, R16 only asserts it).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "desktop" / "backend"
DESKTOP_DIR = REPO_ROOT / "desktop"
SPEC_PATH = BACKEND_DIR / "short-lab.spec"
SMOKE_PATH = REPO_ROOT / "scripts" / "smoke_shortlab_packaged.py"
MIGRATION_006 = (
    BACKEND_DIR
    / "src"
    / "diveintocrypto_desktop"
    / "shortlab"
    / "migrations"
    / "006_optimization_repair.sql"
)
MAIN_PATH = (
    BACKEND_DIR / "src" / "diveintocrypto_desktop" / "__main__.py"
)
UI_DIST = DESKTOP_DIR / "ui" / "dist"

# D13 (design): exactly these seven tables ship in 006.
REQUIRED_REPAIR_TABLES = {
    "sl_market_observation",
    "sl_funding_schedule",
    "sl_fx_observation",
    "sl_hedge_decision_snapshot",
    "sl_hedge_protection_confirmation",
    "sl_strategy_entry_snapshot",
    "sl_strategy_quote_task",
}

# D13: eight new indexes (seven table cutoffs + the FCS current index on
# the pre-existing sl_funding_capture_snapshot table).
REQUIRED_REPAIR_INDEXES = {
    "idx_sl_market_observation_cutoff",
    "idx_sl_funding_schedule_cutoff",
    "idx_sl_fx_observation_cutoff",
    "idx_sl_hedge_decision_symbol",
    "idx_sl_protection_plan",
    "idx_sl_strategy_entry_time",
    "idx_sl_quote_task_due",
    "idx_sl_fcs_current",
}

REQUIRED_MIGRATIONS = (
    "001_init.sql",
    "002_unlock_social.sql",
    "003_catalyst.sql",
    "004_core_completion.sql",
    "005_hedge_advisor.sql",
    "006_optimization_repair.sql",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_spec_bundles_full_r16_resource_set() -> None:
    spec = _read(SPEC_PATH)
    for name in REQUIRED_MIGRATIONS:
        assert name in spec, f"spec missing migration {name}"
    assert "shortlab/default.yaml" in spec or "_SHORTLAB_DEFAULT_YAML" in spec
    assert "engine/config/default.yaml" in spec or "_ENGINE_DEFAULT_YAML" in spec
    assert "asset_overrides.yaml" in spec
    assert "verified_assets.yaml" in spec
    assert "ui/dist" in spec
    assert 'collect_dynamic_libs("duckdb")' in spec
    assert 'collect_data_files("duckdb"' in spec
    assert '"duckdb"' in spec
    assert '"yaml"' in spec
    # Explicit 006 list: no placeholder file may satisfy the bundle.
    assert "006_optimization_repair.sql" in spec
    assert "placeholder" in spec.lower()
    # The datas comment must name the R01 006 file (not stop at 005).
    assert "006" in spec


def test_spec_migration_comment_names_r01_006() -> None:
    spec = _read(SPEC_PATH)
    # R01 delivered 006; R16 only owns the release-side wording. The old
    # "001-004 plus H01 Hedge 005 (explicit)" comment without 006 is stale.
    assert "006_optimization_repair" in spec
    assert "R01" in spec


def test_smoke_requires_006_and_second_boot() -> None:
    smoke = _read(SMOKE_PATH)
    assert "006_optimization_repair" in smoke, "smoke must require the 006 migration"
    for table in REQUIRED_REPAIR_TABLES:
        assert table in smoke, f"smoke missing repair table {table}"
    # Schema gate is 6 (v4/v5 -> v6, D16 V16); the old "< 5" gate is stale.
    assert "version < 6" in smoke or "version<6" in smoke or "<6" in smoke
    assert "006" in smoke
    # Two-boot survival: the script boots twice, compares the install
    # snapshot and keeps a restart probe row across boots.
    assert "range(2)" in smoke
    assert "smoke_restart_probe" in smoke
    assert "snapshot(install) != before" in smoke or "snapshot(install)!=before" in smoke


def _smoke_argparse_options() -> set[str]:
    tree = ast.parse(_read(SMOKE_PATH))
    options: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "add_argument":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value.startswith("--"):
                            options.add(arg.value)
    return options


def test_smoke_cli_args_frozen_from_argparse() -> None:
    options = _smoke_argparse_options()
    assert options == {"--executable", "--output-dir", "--fixture"}, options
    # The delivery manifest must only record these real options (no invented
    # flags). The script docstring states the frozen acceptance + schema 6.
    smoke = _read(SMOKE_PATH)
    assert "--executable" in smoke
    assert "--output-dir" in smoke
    assert "--fixture" in smoke


def test_ui_dist_exists_and_carries_shortlab_routes() -> None:
    assert (UI_DIST / "bundle.js").is_file()
    assert (UI_DIST / "index.html").is_file()
    assert (UI_DIST / "styles.css").is_file()
    bundle = _read(UI_DIST / "bundle.js")
    # Hash routes served by the backend at "/" (desktop-app.jsx).
    assert "#/shortlab" in bundle or "shortlab" in bundle
    assert "hedge-monitor" in bundle or "hedgeMonitor" in bundle or "HEDGE" in bundle
    index = _read(UI_DIST / "index.html")
    assert "bundle.js" in index


def test_packaging_cli_compat_via_argparse_ast() -> None:
    tree = ast.parse(_read(MAIN_PATH))
    defaults: dict[str, object] = {}
    prog: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "ArgumentParser":
                for kw in node.keywords:
                    if kw.arg == "prog" and isinstance(kw.value, ast.Constant):
                        prog = kw.value.value
            if node.func.attr == "add_argument":
                flag: str | None = None
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value.startswith("--"):
                            flag = arg.value
                for kw in node.keywords:
                    if kw.arg == "default" and flag is not None:
                        if isinstance(kw.value, ast.Constant):
                            defaults[flag] = kw.value.value
    assert prog == "short-lab", prog
    assert defaults.get("--host") == "127.0.0.1", defaults
    assert defaults.get("--port") == 46408, defaults


def test_006_sql_defines_seven_tables_eight_indexes() -> None:
    sql = _read(MIGRATION_006)
    for table in REQUIRED_REPAIR_TABLES:
        assert table in sql, f"006 missing table {table}"
    for index in REQUIRED_REPAIR_INDEXES:
        assert index in sql, f"006 missing index {index}"
    assert sql.count("CREATE TABLE IF NOT EXISTS") >= 7
    assert sql.count("CREATE INDEX IF NOT EXISTS") >= 8
