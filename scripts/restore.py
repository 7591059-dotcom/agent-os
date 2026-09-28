#!/usr/bin/env python3
"""Offline restore, guarded by the same exclusive process lock as the server."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

from cryptography.fernet import Fernet, InvalidToken

from backup import ROOT, snapshot, stamp, validate_database
sys.path.insert(0, str(ROOT))
from app.process_lock import lock_file, check_private_file, sync_directory


def master_key(env_path=None):
    if env_path is None and os.getenv('MASTER_KEY'):
        return os.environ['MASTER_KEY']
    path = env_path or ROOT / '.env'
    check_private_file(path)
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith("MASTER_KEY='") and line.endswith("'"):
            return line[len("MASTER_KEY='"):-1]
    raise ValueError('Нет MASTER_KEY. Восстановите исходный .env отдельно от базы.')


def validate_keys(connection, key):
    try:
        cipher = Fernet(key.encode())
        for kind, identifier, data in connection.execute('SELECT kind,id,data FROM documents'):
            doc = json.loads(data)
            if not isinstance(doc, dict):
                raise ValueError('Некорректный документ в копии.')
            encrypted = []
            if kind == 'provider' and doc.get('api_key_encrypted'):
                encrypted.append(doc['api_key_encrypted'])
            if kind == 'connector' and doc.get('secret_encrypted'):
                encrypted.append(doc['secret_encrypted'])
            if kind in ('mcp','decision','crm') and doc.get('token_encrypted'):
                encrypted.append(doc['token_encrypted'])
            if kind == 'setting' and identifier in ('totp_secret', 'encryption_check') and doc.get('value'):
                encrypted.append(doc['value'])
            if kind == 'setting' and identifier == 'totp_pending' and doc.get('value'):
                encrypted.append(doc['value']['secret'])
            for value in encrypted:
                cipher.decrypt(value.encode())
    except (InvalidToken, UnicodeError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError('MASTER_KEY не подходит к зашифрованным данным или копия повреждена.') from None


def neutralize_restored_actions(connection):
    updated = datetime.now(timezone.utc).isoformat()
    connection.execute("DELETE FROM documents WHERE kind='session'")
    connection.execute("DELETE FROM documents WHERE kind IN ('telegram_callback','telegram_session','telegram_outbox','telegram_notice','telegram_chat_reply')")
    connection.execute("DELETE FROM documents WHERE kind='setting' AND (id='totp_pending' OR id LIKE 'telegram_offset_%' OR id LIKE 'bot-chat:%' OR id LIKE 'tool-preview:%')")
    for kind, identifier, data in connection.execute("SELECT kind,id,data FROM documents WHERE kind IN ('task','connector','publication','mcp','decision','crm','automation','tool_request','chat')").fetchall():
        doc = json.loads(data)
        changed = False
        if kind == 'task':
            if doc.get('auto_run'):
                doc['auto_run'] = False
                changed = True
            intake = doc.get('intake')
            if isinstance(intake, dict):
                # Old review links (including web forms) must not authorize a
                # restored snapshot. Paid intake requests need explicit restart.
                intake['revision'] = int(intake.get('revision', 0)) + 1
                if intake.get('state') in ('queued', 'analyzing'):
                    intake.update(state='failed', error='Восстановлено из копии. Проверьте идею и отправьте правки для повторного анализа.')
                    doc['error'] = intake['error']
                # Keep reserved_tokens: Workflow.recover charges interrupted
                # calls conservatively even when their state is now failed.
                changed = True
            if doc.get('status') == 'planning':
                doc.update(status='backlog', auto_run=False)
                changed = True
            elif doc.get('status') == 'running':
                doc.update(status='paused', auto_run=False)
                changed = True
            # Running step reservations remain present: engine.start conservatively
            # accounts for unknown API usage and resets interrupted steps to pending.
        elif kind == 'connector' and doc.get('kind') == 'telegram':
            doc['enabled'] = False
            changed = True
        elif kind in ('mcp','decision','crm','automation'):
            doc['enabled']=False;changed=True
        elif kind=='tool_request' and doc.get('status') in ('pending','checking','executing'):
            doc.update(status='uncertain' if doc['status']=='executing' else 'rejected',error='Восстановлено из копии; согласуйте заново.');changed=True
        elif kind=='chat' and (doc.get('status')=='running' or doc.get('request_started')):
            doc.update(status='stopped' if doc.get('status')=='stopped' else 'failed',error='Восстановлено из копии. Повторите сообщение вручную.',
                usage_uncertain=doc.get('usage_uncertain',False) or doc.get('request_started',True),request_started=False);changed=True
        elif kind == 'publication':
            if doc.get('status') == 'approved':
                doc.update(status='draft', approved_at=None)
                changed = True
            elif doc.get('status') == 'publishing':
                doc.update(status='uncertain', error='Восстановлено из копии. Проверьте публикацию в соцсети перед повтором.')
                changed = True
        if changed:
            doc['updated_at'] = updated
            connection.execute('UPDATE documents SET data=?,updated_at=? WHERE kind=? AND id=?',
                               (json.dumps(doc, ensure_ascii=False), updated, kind, identifier))
    connection.commit()


def main():
    parser = argparse.ArgumentParser(description='Восстановление только после остановки приложения')
    parser.add_argument('backup', type=Path)
    parser.add_argument('--data-dir', type=Path, default=Path(os.getenv('APP_DATA_DIR', ROOT / 'data')))
    parser.add_argument('--env', type=Path, help='Исходный .env с подходящим MASTER_KEY (для локального запуска)')
    parser.add_argument('--confirm-offline', action='store_true', help='Подтверждаю остановку сервера и замену базы')
    args = parser.parse_args()
    if not args.confirm_offline:
        parser.error('Остановите приложение. Затем укажите --confirm-offline для замены базы.')
    temporary = None
    lock = None
    try:
        source = args.backup.resolve(strict=True)
        directory = args.data_dir.resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / 'agentos.sqlite3'
        if source == target.resolve():
            raise ValueError('Источник копии не может совпадать с рабочей базой.')
        lock = os.open(directory / 'application.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_file(lock)
        except BlockingIOError:
            raise ValueError('Приложение работает. Остановите его; восстановление отменено.') from None
        key = master_key(args.env)
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
            validate_database(connection)
            validate_keys(connection, key)
        temporary = directory / f'.restore-{stamp()}.sqlite3'
        snapshot(source, temporary)
        with closing(sqlite3.connect(temporary)) as connection:
            neutralize_restored_actions(connection)
            validate_database(connection)
        if target.exists():
            old_directory = directory / 'backups'
            old_directory.mkdir(mode=0o700, exist_ok=True)
            previous = snapshot(target, old_directory / f'before-restore-{stamp()}.sqlite3')
            print(f'Предыдущая база сохранена: {previous}')
        # The lock guarantees there are no app writers. Old WAL must never attach
        # to the replacement database. Original data already has a separate snapshot.
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(target) + suffix).unlink(missing_ok=True)
        # Windows _commit (os.fsync) requires a descriptor opened for writing.
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        sync_directory(directory)
        print('База восстановлена. Все веб-сессии сброшены; задачи приостановлены.')
        print('Telegram, MCP, JEV, CRM и расписания отключены, очередь команд и одобрения сброшены. Проверьте внешние результаты, затем включите подключения вручную.')
    except (OSError, sqlite3.Error, ValueError, TimeoutError) as exc:
        parser.error(str(exc))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if lock is not None:
            os.close(lock)


if __name__ == '__main__':
    main()
