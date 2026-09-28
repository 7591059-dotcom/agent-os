#!/usr/bin/env python3
"""Consistent online SQLite backup; credentials' master key is deliberately excluded."""
import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def validate_database(connection):
    connection.execute('PRAGMA trusted_schema=OFF')
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('Проверка целостности SQLite не пройдена.')
    row = connection.execute("SELECT type FROM sqlite_master WHERE name='documents'").fetchone()
    expected = ['kind', 'id', 'data', 'created_at', 'updated_at']
    columns = [r[1] for r in connection.execute('PRAGMA table_info(documents)')]
    if row != ('table',) or columns != expected or connection.execute('PRAGMA user_version').fetchone()[0] != 1:
        raise ValueError('Это не база Агентной поддерживаемой версии.')


def snapshot(source, destination):
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    started = time.monotonic()

    def progress(status, remaining, total):
        if time.monotonic() - started > 120:
            raise TimeoutError('Копирование заняло больше 120 секунд; повторите при меньшей нагрузке.')

    try:
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30) as incoming:
            with sqlite3.connect(destination, timeout=30) as outgoing:
                incoming.backup(outgoing, pages=128, progress=progress, sleep=.1)
                outgoing.execute('PRAGMA journal_mode=DELETE')
                validate_database(outgoing)
        # The context manager commits, but SQLite connections also need explicit close.
        incoming.close()
        outgoing.close()
        # Windows _commit (os.fsync) requires a descriptor opened for writing.
        with destination.open('r+b') as stream:
            os.fsync(stream.fileno())
        return destination
    except BaseException:
        for connection in (locals().get('incoming'), locals().get('outgoing')):
            if connection is not None:
                connection.close()
        for suffix in ('', '-journal', '-wal', '-shm'):
            Path(str(destination) + suffix).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description='Онлайн-копия базы SQLite; .env не включается')
    parser.add_argument('--data-dir', type=Path, default=Path(os.getenv('APP_DATA_DIR', ROOT / 'data')))
    parser.add_argument('--output', type=Path, help='Новый файл резервной копии')
    args = parser.parse_args()
    target = args.output or args.data_dir / 'backups' / f'agentos-{stamp()}.sqlite3'
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = snapshot(args.data_dir / 'agentos.sqlite3', target)
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    except (OSError, sqlite3.Error, ValueError, TimeoutError) as exc:
        parser.error(str(exc))
    print(f'Резервная копия: {target}\nSHA-256: {digest}')
    print('Копия содержит тексты задач. .env и MASTER_KEY сохраните отдельно в защищённом месте.')


if __name__ == '__main__':
    main()
