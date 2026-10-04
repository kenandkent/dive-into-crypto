# Short-Lab PR Evidence (design C3.3)

> Paste this table into the PR body. `source_commit` is the exact commit the
> evidence was produced from. `missing / unconfigured / skipped` are reported
> as-is and never rewritten as "all fixed". An expired or unreachable CI
> artifact must be re-run/re-uploaded, not left blank. Local reports never
> stand in for CI or live-network evidence.

| Field | Value |
|---|---|
| task_id | <!-- e.g. F01 --> |
| source_commit | <!-- full 40-char SHA evidence was produced from --> |
| target base SHA | <!-- base-ref the branch is checked against --> |
| C3 acceptance IDs | <!-- e.g. C3.2, C3.3 --> |
| test source paths | <!-- committed test files, e.g. desktop/backend/tests/test_shortlab_core_persistence.py --> |
| test command | <!-- exact command, e.g. `desktop/backend/.venv/bin/python -m pytest -q desktop/backend/tests/test_shortlab_core_persistence.py` --> |
| exit code | <!-- 0, or non-zero with reason --> |
| verification status | <!-- LOCAL_PASS / CI_PASS / UNVERIFIED / FAILED --> |
| live / offline | <!-- offline unless real network evidence is attached --> |
| artifact relative paths | <!-- paths inside the CI artifact, not gitignored local filenames --> |
| file SHA256 | <!-- one line per artifact file --> |
| CI run / artifact links | <!-- null until real CI produces them; never invent URLs --> |

## Ownership check result

<!-- Paste the JSON output of `scripts/check_shortlab_owner.py` (result / task_id / head_sha / issues). -->

```json
{}
```

## Handoff entries

<!-- Downstream interface, source/config versions, file handoffs and capabilities left disabled. -->

- Interfaces:
- Source / config versions:
- File handoffs:
- Not yet enabled:
