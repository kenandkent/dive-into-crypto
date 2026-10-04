"""Regression gate: smoke may never substitute source for a product binary."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('real_smoke', ROOT/'scripts/smoke_shortlab_packaged.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_smoke_requires_binary_and_never_launches_source(tmp_path):
    source = tmp_path/'short-lab.py'
    source.write_text('print(1)')
    with pytest.raises(ValueError):
        smoke.launch_command(source,46408)
    binary=tmp_path/'short-lab.exe'
    binary.write_bytes(b'MZ')
    command=smoke.launch_command(binary,46408)
    assert command[0] == str(binary.resolve())
    assert '-m' not in command and 'uvicorn' not in command
    assert '--no-open' in command


def test_release_requires_frozen_smoke_before_archive_and_publish():
    source=(ROOT/'.github/workflows/release.yml').read_text()
    assert source.index('Verify real frozen product') < source.index('Zip the packaged tree')
    assert '--executable dist/short-lab/short-lab.exe --fixture' in source


def test_readonly_tree_uses_windows_directory_acl(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(smoke.subprocess,'check_output',lambda *a,**k:'"DOMAIN\\user","S-1-5-21-123"\n')
    monkeypatch.setattr(smoke.subprocess,'run',lambda *a,**k:calls.append((a,k)))
    smoke.make_install_readonly(tmp_path,platform='nt')
    assert calls[0][0][0] == ['icacls',str(tmp_path),'/deny','*S-1-5-21-123:(OI)(CI)(W)','/T','/C']
    assert calls[0][1]['check'] is True


def test_readonly_tree_covers_posix_directories_and_files(tmp_path):
    child=tmp_path/'nested';child.mkdir()
    item=child/'resource';item.write_text('x')
    smoke.make_install_readonly(tmp_path,platform='posix')
    assert all((p.stat().st_mode & 0o222)==0 for p in (tmp_path,child,item))
    # Restore permissions for pytest cleanup within the project test directory.
    tmp_path.chmod(0o700);child.chmod(0o700);item.chmod(0o600)


def test_smoke_database_requires_schema_version_and_all_hedge_tables():
    import duckdb
    with duckdb.connect(':memory:') as db:
        db.execute('CREATE TABLE sl_hedge_plan (id INTEGER)')
        db.execute('CREATE TABLE sl_funding_capture_snapshot (id INTEGER)')
        with pytest.raises(RuntimeError,match='005'):
            smoke.validate_product_db(db)
        for table in smoke.REQUIRED_HEDGE_TABLES:
            db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id INTEGER)')
        db.execute('CREATE TABLE sl_schema_version (version INTEGER)')
        db.execute('INSERT INTO sl_schema_version VALUES (4)')
        with pytest.raises(RuntimeError,match='version'):
            smoke.validate_product_db(db)
        db.execute('INSERT INTO sl_schema_version VALUES (5)')
        smoke.validate_product_db(db)


def test_readonly_verification_rejects_a_writable_directory(tmp_path):
    binary=tmp_path/'short-lab';binary.write_bytes(b'\x7fELF')
    with pytest.raises(RuntimeError,match='still allows creating'):
        smoke.verify_install_readonly(tmp_path,binary)
    assert not (tmp_path/'.shortlab-write-probe').exists()


def test_readonly_verification_checks_real_access_under_same_user(tmp_path):
    binary=tmp_path/'short-lab';binary.write_bytes(b'\x7fELF')
    smoke.make_install_readonly(tmp_path,platform='posix')
    try:
        smoke.verify_install_readonly(tmp_path,binary)
    finally:
        tmp_path.chmod(0o700);binary.chmod(0o600)
