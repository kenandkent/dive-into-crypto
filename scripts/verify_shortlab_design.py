"""Offline verification of the integrated design; does not validate product implementation."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / 'docs/ShortLab_Integrated_Upgrade_Design_CN.md'

def sha(data):
    return hashlib.sha256(data).hexdigest()

def run(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT / 'desktop/backend/runtime/verification'):
        raise ValueError('Evidence output must be inside desktop/backend/runtime/verification')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'manifest.json').unlink(missing_ok=True)
    source = DOC.read_text(encoding='utf-8')
    checks = []
    assert source.count('```') % 2 == 0
    for block in re.findall(r'```yaml\n(.*?)```', source, re.S):
        yaml.safe_load(block)
    checks.append('Markdown fences and YAML parsed')
    for number in (1, 2, 3):
        fragment = source[source.index(f'## B附录 F.{number}'):]
        value = json.loads(re.search(r'```json\n(.*?)```', fragment, re.S).group(1))
        expected = re.search(r'SHA256：`([a-f0-9]{64})`', fragment).group(1)
        for raw in (json.dumps(value), json.dumps(value, indent=4).replace('\n', '\r\n')):
            canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
            assert sha(canonical.encode('utf-8')) == expected
        checks.append(f'Canonical hash {number}: {expected}; format invariant')
    blocks = re.findall(r'```sql\n(.*?)```', source, re.S)
    con = duckdb.connect(':memory:')
    migration_root = ROOT / 'desktop/backend/src/diveintocrypto_desktop/shortlab/migrations'
    baseline = [migration_root / name for name in ('001_init.sql', '002_unlock_social.sql', '003_catalyst.sql')]
    for version, path in enumerate(baseline, 1):
        con.execute(path.read_text(encoding='utf-8'))
        con.execute('INSERT INTO sl_schema_version VALUES (?,?)', [version, 0])
    con.execute('BEGIN')
    con.execute(blocks[0])
    con.execute('INSERT INTO sl_schema_version VALUES (4,0)')
    con.execute('COMMIT')
    for stop in range(1, len(blocks)):
        con.execute('BEGIN')
        try:
            for block in blocks[1:stop+1]:
                con.execute(block)
            con.execute('SELECT forced_design_validation_failure')
        except duckdb.Error:
            con.execute('ROLLBACK')
        else:
            raise AssertionError('Failure injection did not fail')
        assert con.execute('SELECT max(version) FROM sl_schema_version').fetchone()[0] == 4
        assert not con.execute("SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'sl_hedge%' OR table_name IN ('sl_spot_venue_snapshot','sl_funding_capture_snapshot')").fetchall()
    for attempt in range(2):
        con.execute('BEGIN')
        for block in blocks[1:]:
            con.execute(block)
        if attempt == 0:
            con.execute('INSERT INTO sl_schema_version VALUES (5,0)')
        con.execute('COMMIT')
    assert con.execute('SELECT max(version) FROM sl_schema_version').fetchone()[0] == 5
    checks.append('DuckDB baseline 001–003 + target 004/005: rollback, version atomicity and repeated execution passed')
    con.close()
    assert re.findall(r'^# B(\d+)\.', source, re.M) == [str(i) for i in range(1,52)]
    assert len(re.findall(r'^\| AC\d+ \|', source, re.M)) == 23
    checks.append('Section and acceptance IDs checked')
    log = output / 'design-validation.txt'
    log.write_text('\n'.join(checks) + '\n', encoding='utf-8')
    tracked = [DOC, ROOT / 'scripts/verify_shortlab_design.py', ROOT / 'desktop/backend/src/diveintocrypto_desktop/__main__.py', ROOT / '.github/workflows/release.yml']
    manifest = {
        'schema_version': 'design-evidence-v1', 'task_id': 'DESIGN_VALIDATION',
        'source_commit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        'utc_time': datetime.now(timezone.utc).isoformat(),
        'implementation_status': 'NOT_IMPLEMENTED', 'verification_status': 'LOCAL_DOC_PASS',
        'ci_run_url': None, 'ci_artifact_url': None, 'live': False,
        'command': ['desktop/backend/.venv/bin/python','scripts/verify_shortlab_design.py','--output-dir',str(output.relative_to(ROOT))],
        'exit_code': 0, 'python_version': sys.version.split()[0], 'duckdb_version': duckdb.__version__,
        'workspace_content': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in tracked + baseline},
        'artifacts': [{'path': str(log.relative_to(ROOT)), 'sha256': sha(log.read_bytes()), 'exit_code': 0}],
        'scope': 'Design parsing and DDL proof only; no business code, CI, live API or packaged executable validation',
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('\n'.join(checks))
    print('Local manifest:', (output/'manifest.json').relative_to(ROOT))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT/'desktop/backend/runtime/verification/design-document')
    args = parser.parse_args()
    run(args.output_dir)
