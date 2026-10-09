"""R00 owner: branch regex, self-check, stage order, alias isolation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_shortlab_owner.py"
REPAIR_POLICY = REPO_ROOT / "docs" / "contracts" / "shortlab_repair_ownership_policy.json"


def _run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _git_repo(tmp_path: Path, branch: str) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _run_git(work, "init")
    _run_git(work, "config", "user.email", "r00-test@example.com")
    _run_git(work, "config", "user.name", "R00 Test")
    _run_git(work, "config", "commit.gpgsign", "false")
    (work / "docs").mkdir(parents=True)
    (work / "docs" / "note.txt").write_text("base\n", encoding="utf-8")
    _run_git(work, "add", ".")
    _run_git(work, "commit", "-m", "base")
    _run_git(work, "checkout", "-b", branch)
    return work


def _commit(work: Path, relpath: str, content: str, message: str) -> str:
    target = work / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _run_git(work, "add", relpath)
    _run_git(work, "commit", "-m", message)
    return _run_git(work, "rev-parse", "HEAD")


def _repair_policy() -> dict:
    return json.loads(REPAIR_POLICY.read_text(encoding="utf-8"))


def _manifest(task_id="R00", branch="codex/r00-repair-freeze", baseline="", **over) -> dict:
    base = {
        "task_id": task_id,
        "branch": branch,
        "baseline_sha": baseline,
        "prerequisite_commits": [],
        "interface_dependencies": [],
        "handoffs": [],
        "allowed_paths": [
            "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py",
            "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_ports.py",
        ],
    }
    base.update(over)
    return base


def _run_check(work: Path, policy: dict, manifest: dict, base_ref: str, head_ref: str = "HEAD"):
    policy_path = work / "policy.json"
    manifest_path = work / "ownership.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--policy", str(policy_path),
         "--manifest", str(manifest_path), "--base-ref", base_ref,
         "--head-ref", head_ref, "--repo", str(work)],
        capture_output=True, text=True,
    )


def test_branch_pattern_accepts_rnn_and_ab_packages():
    policy = _repair_policy()
    import re

    pat = policy["branch_pattern"]
    assert re.match(pat, "codex/r00-repair-freeze")
    assert re.match(pat, "codex/r01-db-migration")
    assert re.match(pat, "codex/r10a-service-boundary")
    assert re.match(pat, "codex/r10b-service-wiring")
    assert re.match(pat, "codex/r16-release")
    assert not re.match(pat, "feature/whatever")
    assert not re.match(pat, "codex/f01-contracts")
    # task_id correspondence is enforced by the checker (upper vs lower).
    assert policy["tasks"][0] == "R00"


def test_r00_self_check_passes_with_empty_prereqs_and_handoffs(tmp_path):
    work = _git_repo(tmp_path, "codex/r00-repair-freeze")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py", "x=1\n", "r00 work")
    manifest = _manifest(baseline=base)
    completed = _run_check(work, _repair_policy(), manifest, base)
    assert completed.returncode == 0, completed.stdout
    assert json.loads(completed.stdout)["issues"] == []


def test_missing_self_coverage_is_exit_1(tmp_path):
    work = _git_repo(tmp_path, "codex/r00-repair-freeze")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py", "x=1\n", "r00")
    # Policy without the touched path -> UNKNOWN_PATH (exit 1), not structure error.
    policy = {"version": 1, "branch_pattern": _repair_policy()["branch_pattern"], "paths": []}
    manifest = _manifest(baseline=base)
    completed = _run_check(work, policy, manifest, base)
    assert completed.returncode == 1
    assert "UNKNOWN_PATH" in completed.stdout or "unknown" in completed.stdout.lower()


def test_policy_structure_error_is_exit_2(tmp_path):
    work = _git_repo(tmp_path, "codex/r00-repair-freeze")
    base = _run_git(work, "rev-parse", "HEAD")
    manifest = _manifest(baseline=base)
    # Missing owner_sequence.
    bad_policy = {"version": 1, "branch_pattern": ".*", "paths": [{"path": "x"}]}
    completed = _run_check(work, bad_policy, manifest, base)
    assert completed.returncode == 2
    # Manifest missing required field.
    good_policy = _repair_policy()
    bad_manifest = dict(manifest)
    del bad_manifest["baseline_sha"]
    completed2 = _run_check(work, good_policy, bad_manifest, base)
    assert completed2.returncode == 2


def test_task_branch_mismatch_rejected(tmp_path):
    work = _git_repo(tmp_path, "codex/r00-repair-freeze")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py", "x=1\n", "r00")
    manifest = _manifest(task_id="R01", branch="codex/r00-repair-freeze", baseline=base)
    completed = _run_check(work, _repair_policy(), manifest, base)
    assert completed.returncode == 1
    assert "TASK_BRANCH_MISMATCH" in completed.stdout


def test_non_r00_touching_r00_file_is_exit_1(tmp_path):
    work = _git_repo(tmp_path, "codex/r01-db-migration")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py", "x=1\n", "r01越权")
    manifest = _manifest(task_id="R01", branch="codex/r01-db-migration", baseline=base,
                         allowed_paths=["desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py"])
    completed = _run_check(work, _repair_policy(), manifest, base)
    assert completed.returncode == 1
    assert "NOT_OWNER" in completed.stdout


def test_production_test_alias_rejected(tmp_path):
    work = _git_repo(tmp_path, "codex/r01-db-migration")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py",
            "from tests.repair_fixtures import make_ports\nx=1\n", "alias")
    policy = _repair_policy()
    manifest = _manifest(task_id="R01", branch="codex/r01-db-migration", baseline=base,
                         allowed_paths=["desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py"])
    completed = _run_check(work, policy, manifest, base)
    assert completed.returncode == 1
    assert "PRODUCTION_TEST_ALIAS" in completed.stdout


def test_sequential_owner_requires_earlier_handoff(tmp_path):
    work = _git_repo(tmp_path, "codex/r11a-budget")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py", "x=1\n", "r11a")
    policy = _repair_policy()
    # request_budget is [R00, R11a]; R11a without R00 handoff must fail stage check.
    manifest = _manifest(task_id="R11a", branch="codex/r11a-budget", baseline=base,
                         allowed_paths=["desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py"])
    completed = _run_check(work, policy, manifest, base)
    assert completed.returncode == 1
    assert "MISSING_STAGE_PREREQUISITE" in completed.stdout
    # With handoff it passes.
    manifest2 = _manifest(task_id="R11a", branch="codex/r11a-budget", baseline=base,
                          allowed_paths=["desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py"],
                          handoffs=["R00"])
    completed2 = _run_check(work, policy, manifest2, base)
    assert completed2.returncode == 0, completed2.stdout


def test_repair_policy_self_coverage_and_owner_sequence():
    policy = _repair_policy()
    paths = {e["path"]: e for e in policy["paths"]}
    for needle in (
        "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py",
        "desktop/backend/src/diveintocrypto_desktop/shortlab/repair_ports.py",
        "scripts/check_shortlab_owner.py",
        "docs/contracts/shortlab_repair_ownership_policy.json",
        "desktop/backend/tests/repair_fixtures.py",
        "desktop/backend/tests/fixtures/repair/*",
        "desktop/backend/tests/test_shortlab_repair_contracts.py",
    ):
        assert needle in paths, needle
        assert paths[needle]["owner_sequence"] == ["R00"] or "R00" in paths[needle]["owner_sequence"]
    assert paths["desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py"]["owner_sequence"] == ["R00", "R11a"]
