"""R16 release-gate static analysis (design D01/D13/D16/D17, plan P06 V16).

Verifies the shippable-contract without running CI:

- Desktop tag ``short-lab-v*`` never triggers Android; Android tag ``v*``
  never triggers Desktop (routing matrix + literal workflow ``if:``).
- The Windows frozen smoke gates the zip and the GitHub Release publish:
  the verify step runs BEFORE zip/publish and none of those three steps
  may use ``if: always()`` (failure blocks the artefacts).
- CI uploads the P06 evidence trees with ``if-no-files-found: error`` so a
  missing bundle fails the run instead of silently passing.
- UI toolchain is pinned for reproducible builds: Node ``>=22 <23``,
  ``@playwright/test`` ``1.56.0`` (dev-only, never bundled), the
  ``test:repair-e2e`` entry, and a ``setup-node`` 22 step in the Desktop
  packaging job. Production deps (esbuild/react) must not drift.
- Production builds never embed test doubles: the spec must not reference
  ``backend/tests/repair_fixtures`` and the esbuild entry must not bundle
  test/e2e/stub/Playwright modules.
- Third-party docs state the default capabilities, enablement and
  boundaries in Chinese; BLOCKED items are never advertised as shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
UI_PKG_PATH = REPO_ROOT / "desktop" / "ui" / "package.json"
UI_LOCK_PATH = REPO_ROOT / "desktop" / "ui" / "package-lock.json"
SPEC_PATH = REPO_ROOT / "desktop" / "backend" / "short-lab.spec"
BUILD_PATH = REPO_ROOT / "desktop" / "ui" / "build.mjs"
README_PATH = REPO_ROOT / "README.md"
DESKTOP_README_PATH = REPO_ROOT / "desktop" / "README.md"
PACKAGING_PATH = REPO_ROOT / "docs" / "packaging.md"
TESTING_PATH = REPO_ROOT / "docs" / "testing.md"
API_PATH = REPO_ROOT / "docs" / "api.md"

ANDROID_IF = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
DESKTOP_IF = (
    "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/short-lab-v'))"
    " || (github.event_name == 'workflow_dispatch' && inputs.package_desktop == true)"
)


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _norm(expr: object) -> str:
    return " ".join(str(expr).split())


def _release() -> dict:
    return _workflow(RELEASE_PATH)


def test_push_tags_cover_both_editions() -> None:
    wf = _release()
    on = wf.get("on", wf.get(True))
    assert set(on["push"]["tags"]) == {"v*", "short-lab-v*"}


def test_android_gate_unchanged() -> None:
    wf = _release()
    assert _norm(wf["jobs"]["release"]["if"]) == _norm(ANDROID_IF)


def test_desktop_gate_matches_design() -> None:
    wf = _release()
    assert _norm(wf["jobs"]["package-desktop"]["if"]) == _norm(DESKTOP_IF)


def _android_runs(event: str, ref: str) -> bool:
    return event == "push" and ref.startswith("refs/tags/v")


def _desktop_runs(event: str, ref: str, package_desktop: bool) -> bool:
    return (event == "push" and ref.startswith("refs/tags/short-lab-v")) or (
        event == "workflow_dispatch" and package_desktop is True
    )


def test_tag_mutual_exclusion_matrix() -> None:
    # short-lab-v* enters Desktop only (the "v" prefix check must not
    # misroute it into Android: "short-lab-v*" does NOT start with "v").
    assert _desktop_runs("push", "refs/tags/short-lab-v1.0.0", False) is True
    assert "refs/tags/short-lab-v1.0.0".startswith("refs/tags/v") is False
    assert _android_runs("push", "refs/tags/short-lab-v1.0.0") is False
    # v* enters Android only.
    assert _android_runs("push", "refs/tags/v0.4.0") is True
    assert _desktop_runs("push", "refs/tags/v0.4.0", False) is False
    # Ticked manual dispatch enters Desktop only; unticked enters neither.
    assert _desktop_runs("workflow_dispatch", "refs/heads/main", True) is True
    assert _android_runs("workflow_dispatch", "refs/heads/main") is False
    assert _desktop_runs("workflow_dispatch", "refs/heads/main", False) is False
    assert _android_runs("workflow_dispatch", "refs/heads/main") is False


def _desktop_steps() -> list[dict]:
    return list(_release()["jobs"]["package-desktop"]["steps"])


def _step_index(steps: list[dict], needle: str) -> int:
    for i, step in enumerate(steps):
        blob = str(step.get("name", "")) + str(step.get("run", ""))
        if needle in blob:
            return i
    raise AssertionError(f"release step not found: {needle}")


def test_windows_frozen_smoke_blocks_zip_and_publish() -> None:
    steps = _desktop_steps()
    verify_idx = _step_index(steps, "Verify real frozen product")
    zip_idx = _step_index(steps, "Zip the packaged tree")
    publish_idx = _step_index(steps, "Publish desktop zip")
    assert verify_idx < zip_idx < publish_idx
    verify_name = str(steps[verify_idx].get("name", ""))
    assert "006" in verify_name or "schema 6" in verify_name.lower()
    # The gate only works when these three steps run on success (no
    # `if: always()` fallback); evidence uploads below are the only steps
    # allowed `if: always()`.
    for idx in (verify_idx, zip_idx, publish_idx):
        assert str(steps[idx].get("if", "success")).strip() != "always()", idx
    blob = "\n".join(
        str(s.get("name", "")) + str(s.get("run", "")) for s in steps
    )
    assert "smoke_shortlab_packaged.py" in blob
    assert "--fixture" in blob
    assert "short-lab-windows-x64.zip" in blob


def test_verification_evidence_upload_covers_p06() -> None:
    steps = _desktop_steps()
    uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert len(uploads) >= 2
    by_name = {str(s.get("with", {}).get("name", "")): s for s in uploads}
    pkg = by_name.get("short-lab-windows-x64")
    assert pkg is not None
    assert pkg["with"]["path"] == "desktop/backend/short-lab-windows-x64.zip"
    assert pkg["with"]["if-no-files-found"] == "error"
    ver_keys = [k for k in by_name if k.startswith("short-lab-verification-")]
    assert ver_keys, list(by_name)
    ver = by_name[ver_keys[0]]
    assert ver["with"]["path"] == "desktop/backend/runtime/verification/"
    assert ver["with"]["if-no-files-found"] == "error"
    assert str(ver.get("if", "")) == "always()"


def test_node22_and_playwright_pinned() -> None:
    pkg = json.loads(UI_PKG_PATH.read_text(encoding="utf-8"))
    assert pkg["engines"]["node"] == ">=22 <23", pkg["engines"]
    dev = pkg.get("devDependencies", {})
    assert dev.get("@playwright/test") == "1.56.0", dev
    # R15a production deps must not drift (esbuild/react pinned by R15a).
    assert dev.get("esbuild") == "^0.24.2", dev
    assert dev.get("react") == "18.3.1", dev
    assert dev.get("react-dom") == "18.3.1", dev
    assert pkg["scripts"].get("test:repair-e2e"), pkg["scripts"]
    lock = json.loads(UI_LOCK_PATH.read_text(encoding="utf-8"))
    assert "node_modules/@playwright/test" in lock.get("packages", {}), "lock missing @playwright/test"
    entry = lock["packages"]["node_modules/@playwright/test"]
    assert entry.get("version") == "1.56.0", entry


def test_release_ui_build_uses_node22() -> None:
    wf = _release()
    steps = wf["jobs"]["package-desktop"]["steps"]
    blob = "\n".join(
        str(s.get("uses", "")) + str(s.get("with", "")) + str(s.get("run", "")) + str(s.get("name", ""))
        for s in steps
    )
    assert "setup-node" in blob
    assert "node-version" in blob and "22" in blob
    assert "npm ci" in blob
    assert "build.mjs" in blob or "npm run build" in blob


def test_production_build_has_no_test_alias() -> None:
    spec = SPEC_PATH.read_text(encoding="utf-8")
    assert "backend/tests/repair_fixtures" not in spec
    assert "repair_fixtures" not in spec
    build = BUILD_PATH.read_text(encoding="utf-8")
    lowered = build.lower()
    assert "playwright" not in lowered
    assert "e2e" not in lowered
    assert "stub" not in lowered


def test_docs_state_capabilities_enablememnt_and_boundaries_zh() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    packaging = PACKAGING_PATH.read_text(encoding="utf-8")
    testing = TESTING_PATH.read_text(encoding="utf-8")
    api = API_PATH.read_text(encoding="utf-8")
    desktop_readme = DESKTOP_README_PATH.read_text(encoding="utf-8")
    combined = "\n".join([readme, packaging, testing, api, desktop_readme])
    # R16 required Chinese boundary phrases (D01.2/D16, never advertised).
    for phrase in (
        "默认能力",
        "启用",
        "正费率",
        "原生",
        "强平",
        "手工",
        "部分退出",
        "数据过期",
        "NO_HEDGE",
        "证据不足",
        "BLOCKED",
    ):
        assert phrase in combined, f"docs missing boundary phrase: {phrase}"
    # Honesty markers must exist; unconfigured/offline rows are never PASS.
    assert "UNCONFIGURED" in combined
    assert "UNVERIFIED" in combined
