#!/usr/bin/env python3
"""AC22 smoke of a REAL frozen ONEDIR executable, never Python source (R16, schema 6).

Run after PyInstaller: python scripts/smoke_shortlab_packaged.py
--executable desktop/backend/dist/short-lab/short-lab.exe --output-dir ...
The copied bundle boots twice from an isolated directory with no PYTHONPATH;
resource presence, UI, schema 6 (001-006, D13 seven tables + eight indexes)
and writable persistent DuckDB are required. The second boot must observe the
same user-data DB file with the first-boot probe row intact, and the
read-only install tree must be byte-identical before/after both boots.
No live trading or fabricated provider results are used.
"""
from __future__ import annotations
import argparse
import csv
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
# R16 frozen resource set (D13/D16 V16): engine + shortlab defaults, both
# identity tables, and the full 001-006 migration chain (R01 delivered
# 006_optimization_repair.sql; R16 only asserts it on the release side).
REQUIRED_RESOURCES = [
    'engine/config/default.yaml', 'shortlab/default.yaml',
    'shortlab/identity/asset_overrides.yaml', 'shortlab/identity/verified_assets.yaml',
    *[f'shortlab/migrations/{name}.sql' for name in (
        '001_init','002_unlock_social','003_catalyst','004_core_completion','005_hedge_advisor',
        '006_optimization_repair')],
]


REQUIRED_HEDGE_TABLES = {
    'sl_hedge_simulation_snapshot', 'sl_hedge_venue_mapping', 'sl_hedge_fill_event',
    'sl_hedge_snapshot_reference', 'sl_hedge_outcome', 'sl_funding_capture_snapshot',
    'sl_spot_venue_snapshot', 'sl_hedge_plan', 'sl_hedge_leg',
    'sl_hedge_monitor_snapshot', 'sl_hedge_alert',
}

# R16 repair tables (D13 seven tables, applied by 006_optimization_repair.sql).
REQUIRED_REPAIR_TABLES = {
    'sl_market_observation',
    'sl_funding_schedule',
    'sl_fx_observation',
    'sl_hedge_decision_snapshot',
    'sl_hedge_protection_confirmation',
    'sl_strategy_entry_snapshot',
    'sl_strategy_quote_task',
}


def validate_product_db(db):
    tables = {r[0] for r in db.execute('SHOW TABLES').fetchall()}
    missing = REQUIRED_HEDGE_TABLES - tables
    if missing:
        raise RuntimeError(f'005 migration tables missing from frozen product: {sorted(missing)}')
    missing_repair = REQUIRED_REPAIR_TABLES - tables
    if missing_repair:
        raise RuntimeError(f'006 repair tables missing from frozen product: {sorted(missing_repair)}')
    version = (db.execute('SELECT max(version) FROM sl_schema_version').fetchone()[0]
               if 'sl_schema_version' in tables else None)
    if version is None or version < 6:
        raise RuntimeError('product schema version did not commit 006')


def snapshot(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def make_install_readonly(directory, *, platform=None):
    """Deny product writes to files AND new paths in the install directory.

    Windows' read-only file bit does not protect directories, so use an
    inherited ACL for the current user SID. A failed ACL command fails smoke.
    """
    directory = Path(directory)
    if (platform or os.name) == 'nt':
        output = subprocess.check_output(
            ['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
        sid = next(csv.reader(io.StringIO(output)))[-1].strip()
        if not sid.startswith('S-1-'):
            raise RuntimeError('cannot determine Windows user SID for read-only install')
        subprocess.run(['icacls', str(directory), '/deny',
                        f'*{sid}:(OI)(CI)(W)', '/T', '/C'], check=True,
                       capture_output=True, text=True)
    else:
        for path in directory.rglob('*'):
            path.chmod(path.stat().st_mode & ~0o222)
        directory.chmod(directory.stat().st_mode & ~0o222)


def verify_install_readonly(directory, executable):
    """Validate OS enforcement using the same user who launches the product."""
    probe = Path(directory) / '.shortlab-write-probe'
    try:
        with probe.open('xb'):
            pass
    except PermissionError:
        pass
    else:
        probe.unlink()
        raise RuntimeError('install directory still allows creating files')
    try:
        with Path(executable).open('r+b'):
            pass
    except PermissionError:
        pass
    else:
        raise RuntimeError('install binary still allows write access')


def launch_command(executable, port):
    """Reject source scripts: acceptance requires the actual compiled product."""
    executable = Path(executable).resolve()
    if not executable.is_file() or executable.suffix in {'.py', '.pyw'}:
        raise ValueError('a built short-lab executable is required')
    if executable.name not in {'short-lab', 'short-lab.exe'}:
        raise ValueError('expected frozen short-lab executable')
    magic = executable.read_bytes()[:4]
    if not (magic[:2] == b'MZ' or magic == b'\x7fELF' or magic in {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe'}):
        raise ValueError('executable is not a native binary')
    return [str(executable), '--no-open', '--host', '127.0.0.1', '--port', str(port)]


def boot(command, env, cwd, log_path):
    with log_path.open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        port = command[-1]
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'frozen product exited: {process.returncode}')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
                        health = json.load(response)
                    if health.get('ok') is True:
                        break
                except (OSError, ValueError):
                    time.sleep(.25)
            else:
                raise RuntimeError('frozen product health timeout')
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as response:
                if b'<html' not in response.read().lower():
                    raise RuntimeError('bundled UI is unavailable')
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/short/health', timeout=5) as response:
                short_health = json.load(response)
            return {'health': health, 'short_health': short_health}
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--fixture', action='store_true', help='serve empty public Futures fixtures locally; disables external optional providers')
    args = parser.parse_args()
    output = Path(args.output_dir).resolve()
    if not output.is_relative_to(ROOT):
        parser.error('output-dir must be inside the project')
    output.mkdir(parents=True, exist_ok=True)
    original = Path(args.executable).resolve()
    launch_command(original, 46408)
    scratch = Path(tempfile.mkdtemp(prefix='packaged-', dir=output))
    report = {'acceptance': 'AC22', 'executable_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
              'source_commit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
              'result': 'FAIL', 'boots': []}
    fixture_server = None
    try:
        install = scratch / 'install'
        shutil.copytree(original.parent, install)
        executable = install / original.name
        before = snapshot(install)
        for resource in REQUIRED_RESOURCES:
            matches = [p for p in install.rglob(Path(resource).name)
                       if p.as_posix().endswith(resource)]
            if not matches:
                raise RuntimeError(f'missing bundled resource: {resource}')
        make_install_readonly(install)
        verify_install_readonly(install, executable)
        data = scratch / 'user-data'
        data.mkdir()
        config = scratch / 'hedge.yaml'
        config.write_text('hedge:\n  enabled: true\nproviders:\n  coingecko:\n    enabled: false\n', encoding='utf-8')
        env = dict(os.environ)
        for key in ('PYTHONPATH','DIVE_MOCK','SHORTLAB_DEMO','DIVE_DEMO'):
            env.pop(key, None)
        env.update(TMPDIR=str(scratch), TEMP=str(scratch), TMP=str(scratch),
                   SHORTLAB_DATA_DIR=str(data), SHORTLAB_CONFIG_PATH=str(config),
                   LOCALAPPDATA=str(scratch / 'local-appdata'))
        if args.fixture:
            class PublicFixture(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = ({'symbols': [], 'rateLimits': []} if 'exchangeInfo' in self.path
                            else {'serverTime': int(time.time()*1000)} if '/time' in self.path else [])
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                def log_message(self, *args):
                    pass
            fixture_server = ThreadingHTTPServer(('127.0.0.1',0), PublicFixture)
            threading.Thread(target=fixture_server.serve_forever, daemon=True).start()
            env['DIVE_FAPI_BASE'] = f'http://127.0.0.1:{fixture_server.server_port}'
            report['fixture'] = 'empty-public-futures-v1; optional providers disabled'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        command = launch_command(executable, port)
        # Parent may inspect the closed DB, but the server always runs the binary.
        import duckdb
        for index in range(2):
            report['boots'].append(boot(command, env, str(scratch), output / f'packaged-boot-{index+1}.txt'))
            db_path = data / 'shortlab.duckdb'
            if not db_path.is_file():
                raise RuntimeError('product did not create its writable DuckDB')
            with duckdb.connect(str(db_path)) as db:
                # R16: every boot must see the full 006 schema (D13 seven
                # tables) at version 6; a 006 failure must never silently
                # present a 005-only ledger as healthy.
                validate_product_db(db)
                if index == 0:
                    db.execute('CREATE TABLE smoke_restart_probe (value INTEGER)')
                    db.execute('INSERT INTO smoke_restart_probe VALUES (22)')
                elif db.execute('SELECT value FROM smoke_restart_probe').fetchall() != [(22,)]:
                    raise RuntimeError('data did not survive second boot')
        # R16 secondary-start assertions: exactly two successful boots, each
        # with a live health probe, sharing one writable DB file while the
        # read-only install tree stays byte-identical.
        if len(report['boots']) != 2:
            raise RuntimeError(f'second boot missing: got {len(report["boots"])} boots')
        for boot_report in report['boots']:
            if boot_report.get('health', {}).get('ok') is not True:
                raise RuntimeError('frozen boot health probe did not report ok:true')
        if snapshot(install) != before:
            raise RuntimeError('product modified read-only install tree')
        report['result'] = 'PASS'
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if fixture_server is not None:
            fixture_server.shutdown()
            fixture_server.server_close()
        (output/'packaged-smoke.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        (output/'packaged-smoke.txt').write_text(json.dumps(report, indent=2), encoding='utf-8')
        # Keep isolated install/data/logs as review evidence; no outside writes.
    print(f"packaged smoke: {report['result']}")
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
