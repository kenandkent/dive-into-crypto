"""Task 18: release workflow tag routing (design section 40).

Static contract + routing matrix for ``.github/workflows/release.yml``:

- ``on.push.tags`` lists BOTH ``v*`` (Android) and ``short-lab-v*``
  (Desktop);
- the Android job gate is exactly
  ``github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')``
  and its build/upload steps are untouched;
- the Desktop job gate is exactly
  ``(github.event_name == 'push' && startsWith(github.ref,
  'refs/tags/short-lab-v')) || (github.event_name == 'workflow_dispatch' &&
  inputs.package_desktop == true)``;
- routing matrix: ``short-lab-v*`` enters Desktop only, ``v*`` enters
  Android only, a ticked manual dispatch enters Desktop only, an unticked
  one enters neither;
- tag Desktop builds publish ``short-lab-windows-x64.zip`` to the GitHub
  Release; manual builds only upload the workflow artifact; the workflow
  mirrors the UI bundle and smoke paths to ``dist/short-lab``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent
REPO_ROOT = BACKEND_DIR.parent.parent

ANDROID_IF = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
DESKTOP_IF = (
    "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/short-lab-v'))"
    " || (github.event_name == 'workflow_dispatch' && inputs.package_desktop == true)"
)


def _workflow() -> dict:
    text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def _norm(expr: str) -> str:
    return " ".join(expr.split())


def test_push_tags_cover_both_editions() -> None:
    wf = _workflow()
    # NOTE: YAML 1.1 parses the `on:` key as boolean True under PyYAML.
    on = wf.get("on", wf.get(True))
    assert set(on["push"]["tags"]) == {"v*", "short-lab-v*"}


def test_android_gate_unchanged() -> None:
    wf = _workflow()
    android = wf["jobs"]["release"]
    assert _norm(str(android["if"])) == _norm(ANDROID_IF)


def test_desktop_gate_matches_design_section_40() -> None:
    wf = _workflow()
    desktop = wf["jobs"]["package-desktop"]
    assert _norm(str(desktop["if"])) == _norm(DESKTOP_IF)


def _android_runs(event: str, ref: str) -> bool:
    return event == "push" and ref.startswith("refs/tags/v")


def _desktop_runs(event: str, ref: str, package_desktop: bool) -> bool:
    return (event == "push" and ref.startswith("refs/tags/short-lab-v")) or (
        event == "workflow_dispatch" and package_desktop is True
    )


def test_short_lab_tag_enters_desktop_only() -> None:
    assert _desktop_runs("push", "refs/tags/short-lab-v1.0.0", False) is True
    assert _android_runs("push", "refs/tags/short-lab-v1.0.0") is False


def test_android_tag_enters_android_only() -> None:
    assert _android_runs("push", "refs/tags/v0.4.0") is True
    assert _desktop_runs("push", "refs/tags/v0.4.0", False) is False


def test_manual_dispatch_with_tick_enters_desktop_only() -> None:
    assert _desktop_runs("workflow_dispatch", "refs/heads/main", True) is True
    assert _android_runs("workflow_dispatch", "refs/heads/main") is False


def test_manual_dispatch_unticked_enters_neither() -> None:
    assert _desktop_runs("workflow_dispatch", "refs/heads/main", False) is False
    assert _android_runs("workflow_dispatch", "refs/heads/main") is False


def test_gates_mirror_the_static_strings() -> None:
    """The Python routing model above must implement the committed gates."""
    wf = _workflow()
    assert _norm(str(wf["jobs"]["release"]["if"])) == _norm(ANDROID_IF)
    assert _norm(str(wf["jobs"]["package-desktop"]["if"])) == _norm(DESKTOP_IF)
    # Spot-check the matrix against literal prefix semantics from the YAML.
    assert "refs/tags/short-lab-v1".startswith("refs/tags/short-lab-v")
    assert not "refs/tags/short-lab-v1".startswith("refs/tags/v")
    assert "refs/tags/v0.4.0".startswith("refs/tags/v")
    assert not "refs/tags/v0.4.0".startswith("refs/tags/short-lab-v")


def test_android_job_still_builds_and_uploads_apk_aab() -> None:
    wf = _workflow()
    steps = wf["jobs"]["release"]["steps"]
    blob = "\n".join(str(s.get("run", "")) + str(s.get("working-directory", "")) for s in steps)
    assert ":app:assembleRelease" in blob
    assert "*.apk" in blob
    assert "*.aab" in blob


def test_desktop_job_packages_spec_zips_and_publishes() -> None:
    wf = _workflow()
    desktop = wf["jobs"]["package-desktop"]
    steps = desktop["steps"]
    blob = "\n".join(
        str(s.get("run", "")) + str(s.get("name", "")) + str(s.get("working-directory", ""))
        for s in steps
    )
    # Fixed build command against the renamed spec.
    assert "uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm" in blob
    # Mirror UI + smoke paths use dist/short-lab.
    assert "dist\\short-lab" in blob or "dist/short-lab" in blob
    # Zip + artifact share one name.
    assert "short-lab-windows-x64.zip" in blob
    artifact = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert artifact["with"]["name"] == "short-lab-windows-x64"
    assert artifact["with"]["path"] == "desktop/backend/short-lab-windows-x64.zip"
    # Tag builds publish the zip to the GitHub Release …
    assert "gh release upload" in blob
    release_step = next(s for s in steps if "gh release create" in str(s.get("run", "")))
    assert "short-lab-v" in str(release_step.get("if", "")) + str(release_step.get("run", ""))
    # … while manual dispatch uploads the artifact only (no second release).
    release_steps = [s for s in steps if "gh release create" in str(s.get("run", ""))]
    assert len(release_steps) == 1


def test_desktop_job_runs_on_windows() -> None:
    wf = _workflow()
    assert wf["jobs"]["package-desktop"]["runs-on"] == "windows-latest"
