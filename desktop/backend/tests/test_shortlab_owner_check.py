"""F01 red tests: Short-Lab file-ownership check tool (design C3.2).

The owner script is read-only: it resolves SHAs, checks ancestry with an
argv-based git invocation, and maps `git diff --name-only base...head`
against the ownership policy + task manifest. Exit codes: 0 pass,
1 ownership/prerequisite/handoff violation, 2 manifest/parameter/git error.

Written red-first: every case fails until scripts/check_shortlab_owner.py
(plus the docs/contracts policy + schema) exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check_shortlab_owner.py"


def _run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _git_repo(tmp_path: Path, branch: str) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _run_git(work, "init")
    _run_git(work, "config", "user.email", "f01-test@example.com")
    _run_git(work, "config", "user.name", "F01 Test")
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


def _policy() -> dict:
    return {
        "version": 1,
        "branch_pattern": r"^codex/[fh]\d+-",
        "paths": [
            {
                "path": "desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py",
                "owner_sequence": ["F01"],
                "prerequisite": [],
                "handoff_required": False,
            },
            {
                "path": "desktop/backend/src/diveintocrypto_desktop/shortlab/migrations/004_core_completion.sql",
                "owner_sequence": ["F01"],
                "prerequisite": [],
                "handoff_required": False,
            },
            {
                "path": "desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py",
                "owner_sequence": ["F02"],
                "prerequisite": ["F01"],
                "handoff_required": True,
            },
        ],
        "default": {"action": "review"},
    }


def _manifest(
    task_id: str = "F01",
    branch: str = "codex/f01-contracts",
    baseline: str = "",
    prerequisites: list[str] | None = None,
    handoffs: list | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "branch": branch,
        "baseline_sha": baseline,
        "prerequisite_commits": prerequisites or [],
        "handoffs": handoffs or [],
        "allowed_paths": [
            "desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py",
            "desktop/backend/src/diveintocrypto_desktop/shortlab/migrations/004_core_completion.sql",
        ],
    }


def _run_check(
    work: Path, policy: dict, manifest: dict, base_ref: str, head_ref: str = "HEAD"
) -> subprocess.CompletedProcess:
    policy_path = work / "policy.json"
    manifest_path = work / "ownership.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy",
            str(policy_path),
            "--manifest",
            str(manifest_path),
            "--base-ref",
            base_ref,
            "--head-ref",
            head_ref,
            "--repo",
            str(work),
        ],
        capture_output=True,
        text=True,
    )


def _passing_repo(tmp_path: Path) -> tuple[Path, dict, str]:
    work = _git_repo(tmp_path, "codex/f01-contracts")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py", "x = 1\n", "f01 work")
    manifest = _manifest(baseline=base)
    return work, manifest, base


def test_owner_check_passes_with_correct_handoff(tmp_path):
    work, manifest, base = _passing_repo(tmp_path)
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["issues"] == []
    assert payload["task_id"] == "F01"


def test_owner_check_rejects_wrong_branch_prefix(tmp_path):
    work = _git_repo(tmp_path, "feature/whatever")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(work, "desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py", "x = 1\n", "work")
    manifest = _manifest(branch="feature/whatever", baseline=base)
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode == 1
    assert "branch" in completed.stdout.lower()


def test_owner_check_rejects_unmerged_prerequisite(tmp_path):
    work = _git_repo(tmp_path, "codex/f02-observations")
    base = _run_git(work, "rev-parse", "HEAD")
    _run_git(work, "checkout", "-b", "side")
    prereq = _commit(work, "docs/note.txt", "base\nprereq\n", "prereq commit")
    _run_git(work, "checkout", "codex/f02-observations")
    _commit(
        work,
        "desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py",
        "y = 1\n",
        "f02 work",
    )
    manifest = _manifest(
        task_id="F02",
        branch="codex/f02-observations",
        baseline=base,
        prerequisites=[prereq],
        handoffs=[{"task_id": "F01"}],
    )
    manifest["allowed_paths"] = [
        "desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py"
    ]
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode == 1
    assert prereq[:12] in completed.stdout


def test_owner_check_rejects_unknown_path(tmp_path):
    work, manifest, base = _passing_repo(tmp_path)
    _commit(work, "totally/unknown/file.txt", "???\n", "unknown path")
    manifest["allowed_paths"].append("totally/unknown/*")
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode == 1
    assert "unknown" in completed.stdout.lower()


def test_owner_check_rejects_cross_owner_shared_file(tmp_path):
    work = _git_repo(tmp_path, "codex/f01-contracts")
    base = _run_git(work, "rev-parse", "HEAD")
    _commit(
        work,
        "desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py",
        "y = 1\n",
        "touch f02 file",
    )
    manifest = _manifest(baseline=base)
    manifest["allowed_paths"].append(
        "desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py"
    )
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode == 1


def test_owner_check_rejects_tampered_baseline(tmp_path):
    work, manifest, base = _passing_repo(tmp_path)
    manifest["baseline_sha"] = "0" * 40
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode in (1, 2)


def test_owner_check_manifest_error_is_exit_2(tmp_path):
    work = _git_repo(tmp_path, "codex/f01-contracts")
    base = _run_git(work, "rev-parse", "HEAD")
    manifest = _manifest(baseline=base)
    del manifest["baseline_sha"]
    completed = _run_check(work, _policy(), manifest, base)
    assert completed.returncode == 2


def test_owner_check_bad_git_ref_is_exit_2(tmp_path):
    work, manifest, _ = _passing_repo(tmp_path)
    completed = _run_check(work, _policy(), manifest, "refs/does/not-exist-12345")
    assert completed.returncode == 2


def test_owner_check_never_mutates_checkout(tmp_path):
    work, manifest, base = _passing_repo(tmp_path)
    before = _run_git(work, "rev-parse", "HEAD")
    _run_check(work, _policy(), manifest, base)
    assert _run_git(work, "rev-parse", "HEAD") == before
    assert _run_git(work, "status", "--short") != "__MUTATED__"


def test_real_policy_and_schema_cover_f01_contracts():
    policy_path = REPO_ROOT / "docs" / "contracts" / "shortlab_ownership_policy.json"
    schema_path = REPO_ROOT / "docs" / "contracts" / "ownership.schema.json"
    template_path = REPO_ROOT / "docs" / "contracts" / "shortlab_pr_evidence_template.md"
    assert policy_path.is_file()
    assert schema_path.is_file()
    assert template_path.is_file()
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert policy["version"] == 1
    assert "required" in schema and "task_id" in schema["required"]
    flat = " ".join(
        entry.get("path", "") for entry in policy.get("paths", [])
    )
    for needle in (
        "scripts/check_shortlab_owner.py",
        "004_core_completion.sql",
        "repository.py",
        "models.py",
        "pyproject.toml",
    ):
        assert needle in flat, needle
    body = template_path.read_text(encoding="utf-8")
    for needle in ("task_id", "source_commit", "exit code", "ownership"):
        assert needle in body.lower(), needle
