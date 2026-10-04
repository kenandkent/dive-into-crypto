#!/usr/bin/env python3
"""Short-Lab file-ownership check (design C3.2, owned by F01).

Read-only gate: validates that every file changed between ``base-ref`` and
``head-ref`` belongs to the manifest's task, that prerequisite commits and
the baseline SHA are ancestors of HEAD, and that the branch name and
handoff coverage match ``docs/contracts/shortlab_ownership_policy.json``.

Exit codes: 0 = pass, 1 = ownership/prerequisite/handoff violation,
2 = manifest/parameter/git error. Stdout is a JSON report with issue
entries (path/code/message) and the checked SHAs; it never contains
personal directories or secrets.

Fixed invocation::

    python scripts/check_shortlab_owner.py --policy \\
        docs/contracts/shortlab_ownership_policy.json --manifest \\
        desktop/backend/runtime/verification/<build-id>/ownership.json \\
        --base-ref <target-base-sha> --head-ref HEAD
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

BRANCH_PATTERN_FALLBACK = r"^codex/[fh]\d+-"
MANIFEST_REQUIRED = (
    "task_id",
    "branch",
    "baseline_sha",
    "prerequisite_commits",
    "handoffs",
    "allowed_paths",
)


def _fail(code: int, message: str, *, task_id=None, head_sha=None, base_ref=None) -> int:
    print(
        json.dumps(
            {
                "result": "error",
                "task_id": task_id,
                "head_sha": head_sha,
                "base_ref": base_ref,
                "issues": [{"path": "", "code": "USAGE", "message": message}],
            }
        )
    )
    return code


def _git(repo: Path, *args: str) -> str:
    """Run a read-only git command via argv (never a shell string)."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


def _is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", maybe_ancestor, descendant],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _handoff_ids(handoffs) -> set:
    ids = set()
    for entry in handoffs or []:
        if isinstance(entry, str):
            ids.add(entry)
        elif isinstance(entry, dict) and entry.get("task_id"):
            ids.add(entry["task_id"])
    return ids


def _matches_any(patterns, path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Short-Lab file-ownership check (C3.2).")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    try:
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail(2, f"cannot load policy: {exc}")
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _fail(2, f"cannot load manifest: {exc}")

    if not isinstance(manifest, dict):
        return _fail(2, "manifest must be a JSON object")
    missing = [key for key in MANIFEST_REQUIRED if key not in manifest]
    if missing:
        return _fail(2, f"manifest misses required fields: {missing}",
                     task_id=manifest.get("task_id"))
    task_id = manifest["task_id"]
    if not isinstance(manifest["prerequisite_commits"], list) or not isinstance(
        manifest["allowed_paths"], list
    ):
        return _fail(2, "prerequisite_commits and allowed_paths must be lists",
                     task_id=task_id)

    try:
        head_sha = _git(repo, "rev-parse", args.head_ref)
        _git(repo, "rev-parse", "--verify", args.base_ref)
    except RuntimeError as exc:
        return _fail(2, f"git ref error: {exc}", task_id=task_id,
                     base_ref=args.base_ref)

    issues: list[dict] = []

    branch_pattern = policy.get("branch_pattern", BRANCH_PATTERN_FALLBACK)
    try:
        branch_ok = re.match(branch_pattern, manifest.get("branch", "") or "") is not None
    except re.error:
        return _fail(2, f"policy branch_pattern is not a valid regex: {branch_pattern}",
                     task_id=task_id, head_sha=head_sha, base_ref=args.base_ref)
    if not branch_ok:
        issues.append(
            {
                "path": "",
                "code": "BAD_BRANCH",
                "message": (
                    f"branch {manifest.get('branch')!r} does not match "
                    f"policy pattern {branch_pattern!r}"
                ),
            }
        )

    baseline = manifest["baseline_sha"]
    if not _is_ancestor(repo, baseline, head_sha):
        issues.append(
            {
                "path": "",
                "code": "BASELINE_NOT_ANCESTOR",
                "message": f"baseline_sha {baseline!r} is not an ancestor of {head_sha[:12]}",
            }
        )

    for commit in manifest["prerequisite_commits"]:
        if not _is_ancestor(repo, commit, head_sha):
            issues.append(
                {
                    "path": "",
                    "code": "PREREQUISITE_NOT_MERGED",
                    "message": f"prerequisite commit {commit!r} is not an ancestor of HEAD",
                }
            )
        elif not _is_ancestor(repo, commit, args.base_ref):
            issues.append(
                {
                    "path": "",
                    "code": "PREREQUISITE_NOT_IN_BASE",
                    "message": (
                        f"prerequisite commit {commit!r} is merged in HEAD but not "
                        f"in base-ref {args.base_ref!r}; target branch has not taken it"
                    ),
                }
            )

    try:
        diff_out = _git(repo, "diff", "--name-only", f"{args.base_ref}...{head_sha}")
    except RuntimeError as exc:
        return _fail(2, f"git diff error: {exc}", task_id=task_id,
                     head_sha=head_sha, base_ref=args.base_ref)
    changed = [line for line in diff_out.splitlines() if line.strip()]

    entries = policy.get("paths", [])
    handoff_ids = _handoff_ids(manifest.get("handoffs"))
    for path in changed:
        covering = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and _matches_any([entry.get("path", "")], path)
        ]
        if not covering:
            issues.append(
                {
                    "path": path,
                    "code": "UNKNOWN_PATH",
                    "message": f"{path!r} matches no policy entry; needs human review",
                }
            )
            continue
        owned = [
            entry
            for entry in covering
            if task_id in (entry.get("owner_sequence") or [])
        ]
        if not owned:
            issues.append(
                {
                    "path": path,
                    "code": "NOT_OWNER",
                    "message": (
                        f"{path!r} is owned by "
                        f"{[e.get('owner_sequence') for e in covering]}, not {task_id!r}"
                    ),
                }
            )
            continue
        if not _matches_any(manifest["allowed_paths"], path):
            issues.append(
                {
                    "path": path,
                    "code": "PATH_NOT_ALLOWED",
                    "message": f"{path!r} is not listed in manifest allowed_paths",
                }
            )
        for entry in owned:
            for prereq in entry.get("prerequisite") or []:
                if prereq not in handoff_ids:
                    issues.append(
                        {
                            "path": path,
                            "code": "MISSING_HANDOFF",
                            "message": (
                                f"{path!r} requires handoff from {prereq!r}, "
                                "which the manifest does not declare"
                            ),
                        }
                    )

    result = "pass" if not issues else "fail"
    print(
        json.dumps(
            {
                "result": result,
                "task_id": task_id,
                "head_sha": head_sha,
                "base_ref": args.base_ref,
                "changed_files": changed,
                "issues": issues,
            }
        )
    )
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
