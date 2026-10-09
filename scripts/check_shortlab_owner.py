#!/usr/bin/env python3
"""Short-Lab file-ownership check (R00 repair freeze, owns F01+R00).

Read-only gate: validates changed files between base-ref and head-ref
against the ownership policy + task manifest, plus R00 production-alias
isolation (D19.6) and branch/task correspondence (D19.2).

Exit codes: 0 = pass, 1 = ownership/prerequisite/handoff/alias violation,
2 = manifest/parameter/git/policy-structure error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

#: R00 branch pattern (D19.2): r00..r16 with optional a/b sub-package.
BRANCH_PATTERN_FALLBACK = r"^codex/r(?:0[0-9]|1[0-6])(?:[ab])?-[a-z0-9][a-z0-9-]*$"
#: Legacy fallback still recognised for old F/H branches in custom policies.
LEGACY_BRANCH_PATTERN = r"^codex/[fh]\d+-"
MANIFEST_REQUIRED = (
    "task_id",
    "branch",
    "baseline_sha",
    "prerequisite_commits",
    "handoffs",
    "allowed_paths",
)

#: P04 task table (R00 freeze). Uppercase task_ids.
KNOWN_TASKS = frozenset(
    {
        "R00", "R01",
        "R02A", "R02B", "R03", "R04", "R05",
        "R06A", "R06B", "R07", "R08A", "R08B", "R09",
        "R10A", "R10B", "R11A", "R11B", "R12",
        "R13A", "R13B", "R14A", "R14B",
        "R15A", "R15B", "R16",
    }
)

_R_BRANCH_TASK = re.compile(r"^codex/r((?:0[0-9]|1[0-6])(?:[ab])?)-", re.IGNORECASE)


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


def _branch_task_id(branch: str) -> str | None:
    m = _R_BRANCH_TASK.match(branch or "")
    if not m:
        return None
    return ("R" + m.group(1)).upper()


def _is_production_path(path: str) -> bool:
    # Production sources that must never import test fakes (D19.6).
    if path.startswith("desktop/backend/src/"):
        return True
    if path.startswith("desktop/ui/src/"):
        return True
    if path == "scripts/check_shortlab_owner.py":
        return False
    if path.startswith("scripts/") and path.endswith(".py"):
        return True
    if path in ("desktop/ui/build.mjs", "desktop/ui/package.json"):
        return True
    return False


def _check_production_alias(repo: Path, head_sha: str, path: str) -> str | None:
    """Return issue message if a production file imports test aliases."""
    try:
        content = subprocess.run(
            ["git", "show", f"{head_sha}:{path}"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        text = content.stdout if content.returncode == 0 else ""
        if not text and Path(repo, path).is_file():
            try:
                text = Path(repo, path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
    except Exception:
        text = ""
    if not text:
        return None
    # Python import/from targeting test trees.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        # Python: import x / from x import y
        m = re.match(r"^\s*(?:import|from)\s+([A-Za-z0-9_\.\/]+)", line)
        if m:
            target = m.group(1)
            if any(
                needle in target
                for needle in (
                    "repair_fixtures",
                    "component-stubs",
                    "component_stubs",
                )
            ):
                return f"{path!r} imports test helper {target!r} (PRODUCTION_TEST_ALIAS)"
            if re.search(r"(^|[\.\/])(tests|test|e2e)([\.\/]|$|_)", target):
                # Allowlist: legitimate production imports never contain these segments.
                return f"{path!r} imports test path {target!r} (PRODUCTION_TEST_ALIAS)"
        # JS: import ... from '...' / export ... from / dynamic import('...')
        for pat in (
            r"""import\s+(?:[^\n]*?\s+from\s+)?['"]([^'"]+)['"]""",
            r"""export\s+[^\n]*?\s+from\s+['"]([^'"]+)['"]""",
            r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""",
            r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
        ):
            for target in re.findall(pat, line):
                if any(
                    needle in target
                    for needle in (
                        "repair_fixtures",
                        "component-stubs",
                        "component_stubs",
                        "/test/",
                        "/tests/",
                        "/e2e/",
                        "desktop/ui/test",
                        "backend/tests",
                    )
                ):
                    return f"{path!r} imports test alias {target!r} (PRODUCTION_TEST_ALIAS)"
    # Build alias config must not map production to test trees.
    if path.endswith("build.mjs") and ("alias" in text or "onResolve" in text or "loader" in text):
        if any(
            needle in text
            for needle in (
                "component-stubs",
                "repair_fixtures",
                "desktop/ui/test",
                "backend/tests",
                "/e2e/",
            )
        ):
            return f"{path!r} configures a test alias for production build (PRODUCTION_TEST_ALIAS)"
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Short-Lab file-ownership check (R00).")
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
    if not isinstance(policy, dict):
        return _fail(2, "policy must be a JSON object")
    # Policy structure (R00): version/branch_pattern/paths required.
    if "paths" not in policy or not isinstance(policy["paths"], list):
        return _fail(2, "policy misses required field: paths")
    for i, entry in enumerate(policy["paths"]):
        if not isinstance(entry, dict):
            return _fail(2, f"policy.paths[{i}] must be an object")
        if "path" not in entry or "owner_sequence" not in entry:
            return _fail(2, f"policy.paths[{i}] misses path/owner_sequence")
        if not isinstance(entry["owner_sequence"], list) or not entry["owner_sequence"]:
            return _fail(2, f"policy.paths[{i}].owner_sequence must be a non-empty list")
        for opt in ("prerequisite", "stage_prerequisites"):
            if opt in entry and entry[opt] is not None and not isinstance(entry[opt], list):
                return _fail(2, f"policy.paths[{i}].{opt} must be a list or null")
    try:
        re.compile(policy.get("branch_pattern", BRANCH_PATTERN_FALLBACK))
    except re.error:
        return _fail(2, f"policy branch_pattern is not a valid regex")

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
    if "handoffs" not in manifest or not isinstance(manifest["handoffs"], list):
        return _fail(2, "handoffs must be a list", task_id=task_id)
    # interface_dependencies is optional for legacy manifests; when present must be a list.
    interface_deps = manifest.get("interface_dependencies", [])
    if interface_deps is None:
        interface_deps = []
    if not isinstance(interface_deps, list):
        return _fail(2, "interface_dependencies must be a list", task_id=task_id)

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
    # R00: branch rNN(a/b) must correspond to task_id (only for r-branches).
    expected_task = _branch_task_id(manifest.get("branch", "") or "")
    if expected_task is not None:
        if not isinstance(task_id, str) or task_id.upper() != expected_task:
            issues.append(
                {
                    "path": "",
                    "code": "TASK_BRANCH_MISMATCH",
                    "message": (
                        f"branch {manifest.get('branch')!r} implies task {expected_task!r}, "
                        f"but manifest task_id is {task_id!r}"
                    ),
                }
            )
        if expected_task not in KNOWN_TASKS:
            issues.append(
                {
                    "path": "",
                    "code": "UNKNOWN_TASK",
                    "message": f"task_id {task_id!r} is not in the P04 task table",
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
    # interface_dependencies never force producer ancestry (D19.2): structure only.

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
            for prereq in entry.get("stage_prerequisites") or []:
                if prereq not in handoff_ids and prereq not in manifest["prerequisite_commits"]:
                    issues.append(
                        {
                            "path": path,
                            "code": "MISSING_STAGE_PREREQUISITE",
                            "message": (
                                f"{path!r} requires stage prerequisite {prereq!r} "
                                "(handoff or merged commit)"
                            ),
                        }
                    )
            # Sequential owners: earlier stages must have handed off.
            seq = entry.get("owner_sequence") or []
            if task_id in seq:
                idx = seq.index(task_id)
                for earlier in seq[:idx]:
                    if earlier not in handoff_ids:
                        issues.append(
                            {
                                "path": path,
                                "code": "MISSING_STAGE_PREREQUISITE",
                                "message": (
                                    f"{path!r} is stage {idx} ({task_id!r}); "
                                    f"earlier owner {earlier!r} has no handoff"
                                ),
                            }
                        )
        # D19.6 production alias isolation (only production paths).
        if _is_production_path(path):
            alias_msg = _check_production_alias(repo, head_sha, path)
            if alias_msg:
                issues.append({"path": path, "code": "PRODUCTION_TEST_ALIAS", "message": alias_msg})

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
