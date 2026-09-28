"""Native process locks and offline utilities; every data/config path is temporary."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from argon2 import PasswordHasher
from cryptography.fernet import Fernet
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def child_environment(directory):
    return {**os.environ, 'APP_DATA_DIR': str(directory), 'PYTHONUTF8': '1',
            'PYTHONIOENCODING': 'utf-8', 'MASTER_KEY': Fernet.generate_key().decode()}


def run_python(arguments, directory):
    return subprocess.run([sys.executable, *map(str, arguments)], cwd=ROOT,
                          env=child_environment(directory), stdin=subprocess.DEVNULL,
                          capture_output=True, encoding='utf-8', timeout=15)


@contextmanager
def held_by_another_process(directory):
    ready = directory / 'lock-ready'
    script = """
import os, sys
from pathlib import Path
from app.process_lock import lock_file
fd = os.open(Path(sys.argv[1]) / 'application.lock', os.O_RDWR | os.O_CREAT, 0o600)
lock_file(fd)
Path(sys.argv[2]).write_text('ready', encoding='utf-8')
sys.stdin.buffer.read(1)
os.close(fd)
"""
    process = subprocess.Popen([sys.executable, '-c', script, str(directory), str(ready)],
                               cwd=ROOT, env=child_environment(directory), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1].decode('utf-8'))
            if time.monotonic() > deadline:
                raise AssertionError('Lock holder failed to become ready')
            time.sleep(.01)
        yield process
    finally:
        try:
            process.communicate(input=b'x' if process.poll() is None else None, timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise


def probe_lock(directory):
    return run_python(['-c', """
import os, sys
from pathlib import Path
from app.process_lock import lock_file
fd = os.open(Path(sys.argv[1]) / 'application.lock', os.O_RDWR | os.O_CREAT, 0o600)
try:
    lock_file(fd)
except BlockingIOError:
    sys.exit(23)
finally:
    os.close(fd)
""", directory], directory)


def create_database(path, documents=()):
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE documents (kind TEXT, id TEXT, data TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY(kind,id))')
        connection.execute('PRAGMA user_version=1')
        for kind, identifier, data in documents:
            connection.execute('INSERT INTO documents VALUES (?,?,?,?,?)',
                               (kind, identifier, json.dumps({'id': identifier, **data}), 'old', 'old'))
    connection.close()


def read_documents(path):
    with sqlite3.connect(path) as connection:
        rows = connection.execute('SELECT kind,id,data FROM documents').fetchall()
    connection.close()
    return {(kind, identifier): json.loads(data) for kind, identifier, data in rows}


def write_config(path, key=None):
    key = key or Fernet.generate_key().decode()
    contents = f"MASTER_KEY='{key}'\nADMIN_PASSWORD_HASH='$argon2id$test-placeholder'\nADMIN_USERNAME='test-owner'\n"
    path.write_text(contents, encoding='utf-8')
    path.chmod(0o600)
    return contents


@pytest.mark.parametrize('abrupt', [False, True])
def test_real_process_lock_blocks_competitor_and_releases(tmp_path, abrupt):
    with held_by_another_process(tmp_path) as holder:
        assert probe_lock(tmp_path).returncode == 23
        if abrupt:
            holder.kill()
            holder.wait(timeout=10)
    result = probe_lock(tmp_path)
    assert result.returncode == 0, result.stderr


def test_local_environment_keeps_dollar_signs_and_backslashes_literal(tmp_path):
    path = tmp_path / '.env'
    expected = {'ADMIN_PASSWORD_HASH': '$argon2id$v=19$m=65536$unchanged',
                'APP_DATA_DIR': str(tmp_path / 'данные $literal'),
                'LITERAL_VALUE': r'$(whoami) # text ${HOME} C:\temporary\folder'}
    path.write_text(''.join(f"{key}='{value}'\n" for key, value in expected.items()), encoding='utf-8')
    path.chmod(0o600)
    result = run_python(['-c', """
import json, os, runpy, sys
from pathlib import Path
module = runpy.run_path(sys.argv[1])
module['load_environment'](Path(sys.argv[2]))
print(json.dumps({key: os.environ[key] for key in sys.argv[3:]}))
""", SCRIPTS / 'run-local.py', path, *expected], tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_restore_refuses_live_database_and_does_not_change_files(tmp_path):
    source = tmp_path / 'backup.sqlite3'
    target = tmp_path / 'agentos.sqlite3'
    create_database(source, [('task', 'backup', {'status': 'done'})])
    create_database(target, [('task', 'current', {'status': 'done'})])
    config = tmp_path / '.env'
    write_config(config)
    before = target.read_bytes()
    with held_by_another_process(tmp_path):
        result = run_python([SCRIPTS / 'restore.py', source, '--data-dir', tmp_path,
                             '--env', config, '--confirm-offline'], tmp_path)
    assert result.returncode != 0
    assert 'Приложение работает' in result.stderr
    assert target.read_bytes() == before
    assert not list(tmp_path.glob('.restore-*'))


def test_online_backup_includes_committed_wal_while_application_is_locked(tmp_path):
    target = tmp_path / 'agentos.sqlite3'
    destination = tmp_path / 'online-copy.sqlite3'
    create_database(target)
    connection = sqlite3.connect(target)
    try:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        connection.execute('INSERT INTO documents VALUES (?,?,?,?,?)',
                           ('task', 'committed', json.dumps({'id': 'committed', 'text': 'Saved in WAL'}), 'now', 'now'))
        connection.commit()
        assert Path(str(target) + '-wal').exists()
        with held_by_another_process(tmp_path):
            result = run_python([SCRIPTS / 'backup.py', '--data-dir', tmp_path, '--output', destination], tmp_path)
        assert result.returncode == 0, result.stderr
        assert read_documents(destination)['task', 'committed']['text'] == 'Saved in WAL'
        assert read_documents(target)['task', 'committed']['text'] == 'Saved in WAL'
    finally:
        connection.close()


def test_restore_replaces_closed_database_and_neutralizes_authority(tmp_path):
    source = tmp_path / 'backup.sqlite3'
    target = tmp_path / 'agentos.sqlite3'
    create_database(source, [
        ('session', 'old-session', {}),
        ('task', 'restored', {'status': 'running', 'auto_run': True}),
        ('mcp', 'server', {'enabled': True}),
        ('automation', 'schedule', {'enabled': True}),
        ('tool_request', 'request', {'status': 'executing'}),
        ('chat', 'legacy', {'status': 'running', 'usage_uncertain': False}),
        ('chat', 'queued', {'status': 'running', 'usage_uncertain': False, 'request_started': False}),
        ('chat', 'sent', {'status': 'running', 'usage_uncertain': False, 'request_started': True}),
    ])
    create_database(target, [('task', 'previous', {'status': 'done'})])
    config = tmp_path / '.env'
    original_config = write_config(config)
    result = run_python([SCRIPTS / 'restore.py', source, '--data-dir', tmp_path,
                         '--env', config, '--confirm-offline'], tmp_path)
    assert result.returncode == 0, result.stderr
    restored = read_documents(target)
    assert ('session', 'old-session') not in restored
    assert restored['task', 'restored']['status'] == 'paused'
    assert restored['task', 'restored']['auto_run'] is False
    assert restored['mcp', 'server']['enabled'] is False
    assert restored['automation', 'schedule']['enabled'] is False
    assert restored['tool_request', 'request']['status'] == 'uncertain'
    for identifier in ('legacy', 'queued', 'sent'):
        assert restored['chat', identifier]['status'] == 'failed'
        assert restored['chat', identifier]['request_started'] is False
        assert restored['chat', identifier]['usage_uncertain'] is (identifier != 'queued')
    previous = list((tmp_path / 'backups').glob('before-restore-*.sqlite3'))
    assert len(previous) == 1
    assert ('task', 'previous') in read_documents(previous[0])
    assert config.read_text(encoding='utf-8') == original_config
    assert not list(tmp_path.glob('.restore-*'))
    assert probe_lock(tmp_path).returncode == 0


def test_restore_wrong_master_key_preserves_current_database(tmp_path):
    source, target = tmp_path / 'backup.sqlite3', tmp_path / 'agentos.sqlite3'
    encrypted = Fernet(Fernet.generate_key()).encrypt(b'test-encryption-check').decode()
    create_database(source, [('setting', 'encryption_check', {'value': encrypted})])
    create_database(target, [('task', 'current', {'status': 'done'})])
    before = target.read_bytes()
    config = tmp_path / '.env'
    write_config(config)
    result = run_python([SCRIPTS / 'restore.py', source, '--data-dir', tmp_path,
                         '--env', config, '--confirm-offline'], tmp_path)
    assert result.returncode != 0
    assert 'MASTER_KEY не подходит' in result.stderr
    assert target.read_bytes() == before
    assert not (tmp_path / 'backups').exists()
    assert probe_lock(tmp_path).returncode == 0


def test_reset_admin_refuses_live_database_before_password_prompt(tmp_path):
    target = tmp_path / 'agentos.sqlite3'
    create_database(target, [('session', 'live-session', {})])
    config = tmp_path / '.env'
    original_config = write_config(config)
    before = target.read_bytes()
    with held_by_another_process(tmp_path):
        result = run_python([SCRIPTS / 'reset-admin.py', '--data-dir', tmp_path,
                             '--env-file', config, '--reset-2fa'], tmp_path)
    assert result.returncode != 0
    assert 'Приложение работает' in result.stderr
    assert 'Новый пароль' not in result.stderr
    assert config.read_text(encoding='utf-8') == original_config
    assert target.read_bytes() == before


@pytest.mark.parametrize('reset_2fa', [False, True])
def test_reset_admin_preserves_master_key_and_releases_handles(tmp_path, reset_2fa):
    target = tmp_path / 'agentos.sqlite3'
    create_database(target, [('session', 'old-session', {}),
                             ('setting', 'totp_secret', {'value': 'encrypted-test-totp'}),
                             ('setting', 'login_global', {'value': [1]})])
    config = tmp_path / '.env'
    master_key = Fernet.generate_key().decode()
    write_config(config, master_key)
    # Replace only terminal input inside this subprocess; never touch a real account.
    result = run_python(['-c', """
import getpass, runpy, sys
from pathlib import Path
script = sys.argv[1]
sys.path.insert(0, str(Path(script).parent))
sys.argv = sys.argv[1:]
getpass.getpass = lambda prompt: 'test-only-password-7284'
runpy.run_path(script, run_name='__main__')
""", SCRIPTS / 'reset-admin.py', '--data-dir', tmp_path, '--env-file', config,
                         *(['--reset-2fa'] if reset_2fa else [])], tmp_path)
    assert result.returncode == 0, result.stderr
    values = dict(line.split('=', 1) for line in config.read_text(encoding='utf-8').splitlines())
    assert values['MASTER_KEY'] == f"'{master_key}'"
    assert values['ADMIN_USERNAME'] == "'test-owner'"
    assert PasswordHasher().verify(values['ADMIN_PASSWORD_HASH'][1:-1], 'test-only-password-7284')
    documents = read_documents(target)
    assert ('session', 'old-session') not in documents
    assert ('setting', 'login_global') not in documents
    assert (('setting', 'totp_secret') not in documents) == reset_2fa
    assert not list(tmp_path.glob('.env-reset-*'))
    assert probe_lock(tmp_path).returncode == 0
    # Windows refuses these replacements if the child retained file handles.
    target.rename(tmp_path / 'database-after-reset.sqlite3')
    config.rename(tmp_path / 'config-after-reset.env')
